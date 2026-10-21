"""Слежение за новыми объявлениями Авито с отправкой в Telegram.

Запуск:
    python avito_watch.py --test        # проверить парсинг, ничего не присылать
    python avito_watch.py --once        # одна проверка с отправкой
    python avito_watch.py               # работать постоянно
    python avito_watch.py --find-tg-id  # узнать chat_id для бота Telegram
"""
import argparse
import json
import re
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import max_client
import telegram_client
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
CONFIG = HERE / "config.json"
STATE = HERE / "state.json"
PROFILE = HERE / "profile"
LOG = HERE / "watch.log"

ITEM_JS = """
(limit) => {
  const out = [];
  const nodes = document.querySelectorAll('[data-marker="item"]');
  for (const n of nodes) {
    const a = n.querySelector('a[data-marker="item-title"]')
           || n.querySelector('a[itemprop="url"]')
           || n.querySelector('h3 a')
           || n.querySelector('a[href*="-"]');
    if (!a) continue;
    const href = a.href || a.getAttribute('href') || '';
    const title = (a.getAttribute('title') || a.textContent || '').trim().replace(/\\s+/g, ' ');
    const priceMeta = n.querySelector('meta[itemprop="price"]');
    let price = priceMeta ? (priceMeta.getAttribute('content') || '') : '';
    if (!price) {
      const pv = n.querySelector('[data-marker="item-price-value"]');
      if (pv) price = (pv.textContent || '').replace(/[^0-9]/g, '');
    }
    if (!price) {
      const pe = n.querySelector('[data-marker="item-price"]');
      if (pe) price = (pe.textContent || '').replace(/[^0-9]/g, '');
    }
    const img = n.querySelector('img[itemprop="image"]') || n.querySelector('img');
    const image = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
    const loc = n.querySelector('[data-marker="item-location"]') || n.querySelector('[class*="geo-address"]');
    const location = loc ? (loc.textContent || '').trim().replace(/\\s+/g, ' ') : '';
    const dt = n.querySelector('[data-marker="item-date"]');
    const date = dt ? (dt.textContent || '').trim() : '';
    out.push({ href, title, price, image, location, date });
    if (out.length >= limit) break;
  }
  return out;
}
"""

BLOCK_WORDS = ("подтвердите, что вы не робот", "доступ ограничен", "проверка браузера")


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"initialized": False, "seen": []}


def save_state(state: dict) -> None:
    state["seen"] = state["seen"][-3000:]
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def clean_url(href: str) -> str:
    """Убираем utm-метки и служебный context из ссылки."""
    try:
        p = urlparse(href)
        if not p.netloc:
            p = urlparse("https://www.avito.ru" + href)
        return urlunparse((p.scheme or "https", p.netloc, p.path, "", "", ""))
    except Exception:
        return href


def item_id(href: str) -> str:
    m = re.search(r"[_-](\d{6,})(?:\?|$|/)", href) or re.search(r"/(\d{6,})(?:\?|$|/)", href)
    return m.group(1) if m else href


def parse_price(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value or "")
    return int(digits) if digits else None


def passes_filters(item: dict, f: dict) -> bool:
    text = (item["title"] + " " + item["location"]).lower()
    for w in f.get("exclude") or []:
        if w.lower() in text:
            return False
    keywords = f.get("keywords") or []
    if keywords and not any(w.lower() in text for w in keywords):
        return False
    price = parse_price(item["price"])
    if price is not None:
        if f.get("min_price") and price < f["min_price"]:
            return False
        if f.get("max_price") and price > f["max_price"]:
            return False
    return True


def format_message(item: dict, search_name: str) -> str:
    price = parse_price(item["price"])
    price_s = f"{price:,}".replace(",", " ") + " ₽" if price else "цена не указана"
    lines = [f"🆕 {item['title']}", f"💰 {price_s}"]
    if item.get("location"):
        lines.append(f"📍 {item['location']}")
    lines.append("")
    lines.append(clean_url(item["href"]))
    return "\n".join(lines)


def launch_browser(pw, headless: bool):
    PROFILE.mkdir(exist_ok=True)
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        channel="chrome",
        headless=headless,
        locale="ru-RU",
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )


def fetch_items(page, url: str, limit: int) -> tuple[list[dict], str, int]:
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    status = resp.status if resp else 0
    try:
        page.wait_for_selector('[data-marker="item"]', timeout=15000)
    except Exception:
        pass
    html = (page.inner_text("body") or "")[:5000].lower()
    if any(w in html for w in BLOCK_WORDS):
        return [], "block", status
    page.mouse.wheel(0, 1200)
    time.sleep(1.2)
    items = page.evaluate(ITEM_JS, limit)
    title = page.title()
    seen, unique = set(), []
    for it in items:
        iid = item_id(it["href"])
        if iid in seen:
            continue
        seen.add(iid)
        it["id"] = iid
        unique.append(it)
    return unique, title, status


def download_image(page, url: str) -> bytes | None:
    """Скачивает фото объявления через браузер (с его куками)."""
    if not url or not url.startswith("http"):
        return None
    try:
        r = page.context.request.get(url, timeout=20000)
        if r.status != 200:
            return None
        ctype = (r.headers or {}).get("content-type", "")
        if "image" not in ctype:
            return None
        data = r.body()
        if not data or len(data) > telegram_client.PHOTO_LIMIT:
            return None
        return data
    except Exception as e:
        log(f"фото не скачалось: {type(e).__name__}")
        return None


def send_item(cfg: dict, page, item: dict, search_name: str) -> bool:
    """Отправляет объявление: фото с подписью, при сбое — просто текст."""
    caption = format_message(item, search_name)
    tg = cfg.get("telegram") or {}
    if cfg.get("send_photos", True) and tg.get("token") and tg.get("chat_id"):
        photo = download_image(page, item.get("image", ""))
        if photo:
            res = telegram_client.send_photo(tg["token"], tg["chat_id"], photo, caption)
            if res.get("ok"):
                return True
            log(f"фото не принято ({res.get('description')}), отправляю текстом")
    return notify(cfg, caption)


def notify(cfg: dict, text: str) -> bool:
    tg = cfg.get("telegram") or {}
    if tg.get("token") and tg.get("chat_id"):
        res = telegram_client.send(tg["token"], tg["chat_id"], text)
        if res.get("ok"):
            return True
        log(f"Telegram не принял сообщение: {res.get('description')}")
        return False

    m = cfg.get("max") or {}
    if m.get("token") and (m.get("user_id") or m.get("chat_id")):
        res = max_client.send(m["token"], text, user_id=m.get("user_id"), chat_id=m.get("chat_id"))
        if "_error" in res:
            log(f"MAX не принял сообщение: {res['_error']}")
            return False
        return True

    log("мессенджер не настроен: впишите telegram.token и telegram.chat_id")
    return False


def check_once(cfg: dict, page, state: dict, test: bool) -> int:
    url = cfg["search_url"]
    items, title, status = fetch_items(page, url, cfg.get("max_items_per_check", 50))
    if items == [] and status not in (200, 0):
        log(f"страница не открылась (HTTP {status})")
    if not items:
        log("объявления не найдены (возможно, блокировка или пустая выдача)")
        if not test:
            notify(cfg, "⚠️ Авито не отдал объявления. Возможно, временная блокировка — попробую позже.")
        return 0

    f = cfg.get("filters") or {}
    items = [i for i in items if passes_filters(i, f)]
    log(f"найдено на странице: {len(items)} | «{title[:80]}»")

    if test:
        for it in items[:15]:
            print(f"  {it['id']} | {it['price'] or '—'} | {it['title'][:70]} | {it.get('location','')}")
        return len(items)

    if not state.get("initialized"):
        state["seen"] = [i["id"] for i in items]
        state["initialized"] = True
        save_state(state)
        log("первый запуск: текущие объявления запомнены, дальше буду присылать только новые")
        if cfg.get("notify_on_start"):
            notify(cfg, f"✅ Слежение запущено.\n«{title[:80]}»\nСейчас в выдаче: {len(items)}.\nНовые объявления буду присылать сюда.")
        return 0

    # сначала добиваем то, что не ушло в прошлый раз
    pending = state.get("pending") or []
    if pending:
        left = []
        for it in pending:
            if send_item(cfg, page, it, title):
                state["seen"].append(it["id"])
                log(f"дослано из очереди: {it['id']}")
            else:
                left.append(it)
        state["pending"] = left
        if left:
            log(f"очередь: осталось {len(left)}, повторю в следующую проверку")

    seen = set(state["seen"])
    fresh = [i for i in items if i["id"] not in seen]
    fresh.reverse()  # от старых к новым

    # После долгого простоя новых может быть много — шлём одним дайджестом,
    # чтобы не заваливать чат десятками сообщений.
    threshold = int(cfg.get("digest_threshold", 8))
    if len(fresh) > threshold:
        lines = [f"📦 Новых объявлений: {len(fresh)}", ""]
        for it in fresh[-15:]:
            price = parse_price(it["price"])
            price_s = f"{price:,}".replace(",", " ") + " ₽" if price else "цена не указана"
            where = f" · {it['location']}" if it.get("location") else ""
            lines.append(f"• {it['title'][:70]}\n  {price_s}{where}\n  {clean_url(it['href'])}")
        if len(fresh) > 15:
            lines.append(f"\n…и ещё {len(fresh) - 15}")
        ok = notify(cfg, "\n".join(lines)[:4000])
        log(f"дайджест: {len(fresh)} новых, {'отправлен' if ok else 'в очередь'}")
        if ok:
            state["seen"].extend(i["id"] for i in fresh)
        else:
            state.setdefault("pending", []).extend(fresh)
            state["pending"] = state["pending"][-200:]
        save_state(state)
        return len(fresh)

    for it in fresh:
        ok = send_item(cfg, page, it, title)
        log(f"{'отправлено' if ok else 'в очередь'}: {it['id']} {it['title'][:60]}")
        if ok:
            state["seen"].append(it["id"])
        else:
            state.setdefault("pending", []).append(it)
            state["pending"] = state["pending"][-200:]
        time.sleep(0.6)
    if fresh:
        save_state(state)
    return len(fresh)


def find_tg_id(cfg: dict) -> int:
    token = (cfg.get("telegram") or {}).get("token")
    if not token:
        print("Сначала впишите токен бота Telegram в config.json → telegram.token")
        return 1
    info = telegram_client.me(token)
    if not info.get("username"):
        print("Токен не принят:", info.get("description") or info)
        return 1
    print(f"Бот: {info.get('first_name')} (@{info.get('username')})")
    print("Напишите боту любое сообщение в Telegram… жду 90 секунд")
    found = telegram_client.find_chat_id(token, seconds=90)
    if "_error" in found:
        print(found["_error"])
        return 1
    cfg.setdefault("telegram", {}).update({"chat_id": found["chat_id"]})
    save_config(cfg)
    print(f"Готово: chat_id={found['chat_id']} ({found.get('name')})")
    return 0


def find_max_id(cfg: dict) -> int:
    token = (cfg.get("max") or {}).get("token")
    if not token:
        print("Сначала впишите токен бота MAX в config.json → max.token")
        return 1
    info = max_client.me(token)
    if "_error" in info:
        print("Токен не принят:", info)
        return 1
    print(f"Бот: {info.get('name')} (@{info.get('username')})")
    print("Напишите боту любое сообщение в MAX… жду 90 секунд")
    found = max_client.find_dialog(token, seconds=90)
    if "_error" in found:
        print(found["_error"])
        return 1
    cfg.setdefault("max", {}).update({"user_id": found["user_id"], "chat_id": found.get("chat_id")})
    save_config(cfg)
    print(f"Готово: user_id={found['user_id']} chat_id={found.get('chat_id')}")
    return 0


LOCK_PORT = 48765
_lock_socket = None


def take_lock() -> bool:
    """Не даёт запустить второй экземпляр: они мешают друг другу."""
    global _lock_socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _lock_socket = s
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="проверить парсинг без отправки")
    ap.add_argument("--once", action="store_true", help="одна проверка с отправкой")
    ap.add_argument("--find-max-id", action="store_true", help="определить user_id бота MAX")
    ap.add_argument("--find-tg-id", action="store_true", help="определить chat_id бота Telegram")
    ap.add_argument("--url", help="ссылка на выдачу Авито (переопределить из конфига)")
    ap.add_argument("--headless", action="store_true", help="без окна браузера")
    args = ap.parse_args()

    if not (args.find_max_id or args.find_tg_id) and not take_lock():
        print("Уже работает другой экземпляр слежения — второй не нужен.")
        return 1

    cfg = load_config()
    tg_cfg = cfg.get("telegram") or {}
    telegram_client.API_BASE = tg_cfg.get("api_base") or ""
    telegram_client.RELAY_SECRET = tg_cfg.get("relay_secret") or ""
    if args.headless:
        cfg["headless"] = True
    if args.url:
        cfg["search_url"] = args.url
        save_config(cfg)

    if args.find_max_id:
        return find_max_id(cfg)

    if args.find_tg_id:
        return find_tg_id(cfg)

    if not cfg.get("search_url"):
        print("Впишите ссылку на поиск Авито в avito/config.json → search_url")
        print("или запустите: python avito_watch.py --url \"https://www.avito.ru/...\"")
        return 1

    state = load_state()
    interval = int(cfg.get("interval_seconds", 300))
    headless = bool(cfg.get("headless", False))

    with sync_playwright() as pw:
        ctx = launch_browser(pw, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if args.test or args.once:
                check_once(cfg, page, state, test=args.test)
                return 0
            log(f"слежение запущено, интервал {interval} с")
            while True:
                try:
                    check_once(cfg, page, state, test=False)
                except Exception as e:
                    log(f"ошибка проверки: {type(e).__name__}: {e}")
                time.sleep(interval)
        except KeyboardInterrupt:
            log("остановлено пользователем")
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
