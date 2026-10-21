"""Клиент Bot API мессенджера MAX (platform-api2.max.ru)."""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://platform-api2.max.ru"


def _request(token: str, path: str, method: str = "GET", body: dict | None = None, timeout: int = 30) -> dict:
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}", "_body": e.read().decode("utf-8", "replace")[:400]}
    except ssl.SSLError:
        # цепочка сертификатов Минцифры может отсутствовать в системе
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def me(token: str) -> dict:
    return _request(token, "/me")


def send(token: str, text: str, user_id: int | None = None, chat_id: int | None = None) -> dict:
    if not user_id and not chat_id:
        return {"_error": "не задан user_id или chat_id"}
    target = f"user_id={user_id}" if user_id else f"chat_id={chat_id}"
    return _request(token, f"/messages?{target}", method="POST", body={"text": text[:4000]})


def find_dialog(token: str, seconds: int = 90) -> dict:
    """Ждёт сообщение боту и возвращает id отправителя и чата."""
    marker = None
    end = None
    import time

    end = time.time() + seconds
    while time.time() < end:
        q = {"timeout": 20}
        if marker:
            q["marker"] = marker
        data = _request(token, "/updates?" + urllib.parse.urlencode(q), timeout=25)
        if "_error" in data:
            return data
        marker = data.get("marker", marker)
        for upd in data.get("updates", []):
            msg = upd.get("message") or {}
            sender = msg.get("sender") or {}
            recipient = msg.get("recipient") or {}
            uid = sender.get("user_id")
            if uid:
                return {
                    "user_id": uid,
                    "chat_id": recipient.get("chat_id"),
                    "chat_type": recipient.get("chat_type"),
                    "name": sender.get("name") or sender.get("first_name"),
                }
    return {"_error": "сообщений не было — напишите боту в MAX и повторите"}
