"""Клиент Telegram Bot API с обходом блокировки по SNI.

Провайдер рвёт TLS-рукопожатие, если в нём указано имя api.telegram.org.
Поэтому при недоступности прямого запроса используется тот же сервер Telegram
по IP с нейтральным SNI, но с полноценной проверкой сертификата: цепочка
проверяется стандартным образом, а имя в сертификате сверяется вручную.

Поддерживается релей (Cloudflare Worker): если задан API_BASE, все запросы
идут через него.
"""
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

API_HOST = "api.telegram.org"
FALLBACK_IPS = ("149.154.167.220", "149.154.167.221", "149.154.167.222")
SNI_MASK = "example.com"
TELEGRAM_NAMES = ("telegram.org", "*.telegram.org")

API_BASE = ""
RELAY_SECRET = ""
_DIRECT_WORKS = True

PHOTO_LIMIT = 10 * 1024 * 1024


def _parse(raw: bytes) -> dict:
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {"ok": False, "description": "не JSON в ответе Telegram"}


def _direct(token: str, method: str, body: bytes, ctype: str, timeout: int):
    url = f"https://{API_HOST}/bot{token}/{method}"
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _parse(r.read()), None
    except urllib.error.HTTPError as e:
        res = _parse(e.read())
        if "ok" in res:
            return res, None
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _via_ip(ip: str, token: str, method: str, body: bytes, ctype: str, timeout: int):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # имя проверяем вручную ниже
    ctx.verify_mode = ssl.CERT_REQUIRED  # цепочка сертификатов проверяется

    raw = socket.create_connection((ip, 443), timeout=timeout)
    tls = ctx.wrap_socket(raw, server_hostname=SNI_MASK)
    try:
        cert = tls.getpeercert() or {}
        names = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
        if not any(n in TELEGRAM_NAMES for n in names):
            return None, f"сертификат не принадлежит Telegram: {names}"

        head = (
            f"POST /bot{token}/{method} HTTP/1.1\r\n"
            f"Host: {API_HOST}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "User-Agent: avito-watch\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        tls.sendall(head + body)

        chunks = []
        while True:
            part = tls.recv(65536)
            if not part:
                break
            chunks.append(part)
    finally:
        tls.close()

    data = b"".join(chunks)
    head_raw, _, payload = data.partition(b"\r\n\r\n")
    if b" 200 " not in head_raw.split(b"\r\n")[0] and b" 4" not in head_raw.split(b"\r\n")[0]:
        return None, head_raw.split(b"\r\n")[0].decode("latin-1", "replace")
    return _parse(payload), None


def _request(token: str, method: str, body: bytes, ctype: str, timeout: int = 30) -> dict:
    global _DIRECT_WORKS

    if API_BASE:
        url = f"{API_BASE.rstrip('/')}/bot{token}/{method}"
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
        if RELAY_SECRET:
            req.add_header("X-Relay-Secret", RELAY_SECRET)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return _parse(r.read())
        except urllib.error.HTTPError as e:
            return _parse(e.read())
        except Exception as e:
            return {"ok": False, "description": f"релей недоступен: {type(e).__name__}: {e}"}

    last = ""
    if _DIRECT_WORKS:
        res, err = _direct(token, method, body, ctype, min(timeout, 8))
        if res is not None:
            return res
        _DIRECT_WORKS = False
        last = err or ""

    for ip in FALLBACK_IPS:
        res, err = _via_ip(ip, token, method, body, ctype, timeout)
        if res is not None:
            return res
        last = err or last

    return {"ok": False, "description": f"Telegram недоступен ({last})"}


def _call(token: str, method: str, params: dict | None = None, timeout: int = 30) -> dict:
    body = urllib.parse.urlencode(params or {}).encode("utf-8")
    return _request(token, method, body, "application/x-www-form-urlencoded", timeout)


def me(token: str) -> dict:
    res = _call(token, "getMe", timeout=15)
    return res.get("result", res)


def send(token: str, chat_id: int | str, text: str) -> dict:
    return _call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": "true",
    })


def _image_meta(photo: bytes) -> tuple[str, str]:
    if photo.startswith(b"\xff\xd8\xff"):
        return "photo.jpg", "image/jpeg"
    if photo.startswith(b"\x89PNG"):
        return "photo.png", "image/png"
    if photo[:4] == b"RIFF" and photo[8:12] == b"WEBP":
        return "photo.webp", "image/webp"
    return "photo.jpg", "image/jpeg"


def send_photo(token: str, chat_id: int | str, photo: bytes, caption: str = "") -> dict:
    """Отправляет картинку с подписью (multipart/form-data)."""
    if not photo or len(photo) > PHOTO_LIMIT:
        return {"ok": False, "description": "картинка пустая или слишком большая"}

    boundary = "----avito" + uuid.uuid4().hex
    name, ctype = _image_meta(photo)
    parts = []

    def field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode("utf-8"))
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")

    field("chat_id", chat_id)
    if caption:
        field("caption", caption[:1024])
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
        f"filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode("utf-8")
    )
    parts.append(photo)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    return _request(token, "sendPhoto", b"".join(parts), f"multipart/form-data; boundary={boundary}", timeout=60)


def find_chat_id(token: str, seconds: int = 90) -> dict:
    """Ждёт сообщение боту и возвращает chat_id отправителя."""
    import time

    end = time.time() + seconds
    offset = 0

    # Если у бота был webhook, getUpdates не работает — снимаем его,
    # накопленные сообщения при этом не теряются.
    for attempt in range(3):
        res = _call(token, "deleteWebhook", {"drop_pending_updates": "false"}, timeout=15)
        if res.get("ok"):
            break
        if res.get("error_code"):
            return {"_error": res.get("description", "не удалось снять webhook")}
        if attempt == 2:
            return {"_error": "не удалось связаться с Telegram — проверьте VPN"}
        time.sleep(3)

    while time.time() < end:
        res = _call(token, "getUpdates", {"timeout": 20, "offset": offset}, timeout=30)
        if not res.get("ok"):
            if res.get("error_code"):
                return {"_error": res.get("description", "ошибка Telegram API")}
            time.sleep(2)
            continue
        for upd in res.get("result", []):
            offset = max(offset, upd.get("update_id", 0) + 1)
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                return {
                    "chat_id": chat["id"],
                    "type": chat.get("type"),
                    "name": chat.get("title") or chat.get("username") or chat.get("first_name"),
                }
    return {"_error": "сообщений не было — напишите боту в Telegram и повторите"}
