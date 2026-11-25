"""Render the isolated UX prototype at fixed viewports, with no external traffic."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "visual-output"

def main():
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width in (360, 390, 768, 1440):
            page = browser.new_page(viewport={"width": width, "height": 900}, device_scale_factor=1)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", lambda route: route.abort() if route.request.url.startswith(("http:", "https:")) else route.continue_())
            page.goto((ROOT / "index.html").as_uri())
            nav = page.locator("#mobile" if width <= 640 else "#nav")
            view = page.locator("#view")

            def shot(label):
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=OUT / f"{width}-{label}.png", full_page=True, animations="disabled")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"overflow: {width} {label}"
                assert not errors, errors
                print(f"PASS {width} {label}", flush=True)

            shot("client-home-light")
            nav.get_by_role("button", name="Календарь", exact=True).click()
            shot("client-calendar-week")
            view.get_by_role("button", name="Месяц", exact=True).click()
            shot("client-calendar-month")
            view.get_by_role("button", name="+ Создать пост", exact=True).click()
            view.locator("#post-text").fill("Приглашаем на открытую встречу в нашей студии.")
            view.locator('[data-target="tg"]').check()
            shot("client-editor-light")
            page.locator("#theme").click()
            shot("client-editor-dark")
            page.locator("#role").select_option("admin")
            page.locator("#profile").select_option("yoga")
            shot("admin-home-dark")
            page.locator("#theme").click()
            shot("admin-home-light")
            nav.get_by_role("button", name="Модерация", exact=True).click()
            shot("admin-moderation")
            if width <= 640:
                nav.get_by_role("button", name="Ещё").click()
                view.get_by_role("button", name="Клиенты", exact=True).click()
            else:
                nav.get_by_role("button", name="Клиенты", exact=True).click()
            view.get_by_role("button", name="+ Пригласить клиента").click()
            shot("admin-invite")
            view.get_by_role("button", name="Создать приглашение (демо)").click()
            view.get_by_role("button", name="Открыть как клиент").click()
            shot("client-accept")
            view.get_by_role("button", name="Создать аккаунт (демо)").click()
            shot("client-onboarding")
            view.locator('[data-action="setup:tg"]').click()
            shot("client-telegram-guide")
            for _ in range(3):
                view.get_by_role("button", name="Далее", exact=True).click()
            view.get_by_role("button", name="Симулировать успешную проверку").click()
            shot("client-connected")
            view.get_by_role("button", name="Создать первую публикацию").click()
            shot("client-first-editor")
            page.close()
        browser.close()

if __name__ == "__main__":
    main()
