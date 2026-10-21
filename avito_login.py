"""Открывает Авито в видимом окне: пройти проверку и сохранить сессию в профиль."""
import time

import avito_watch as a

cfg = a.load_config()
url = cfg["search_url"]
print("Открываю Chrome. Если Авито попросит подтвердить, что вы не робот — сделайте это в окне.", flush=True)

with a.sync_playwright() as pw:
    ctx = a.launch_browser(pw, headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    deadline = time.time() + 300
    last = -1
    while time.time() < deadline:
        try:
            n = page.locator('[data-marker="item"]').count()
        except Exception:
            n = 0
        if n != last:
            print(f"объявлений на странице: {n}", flush=True)
            last = n
        if n > 0:
            break
        time.sleep(2)

    time.sleep(5)  # дать браузеру сохранить куки
    ctx.close()

print("готово", flush=True)
