"""Isolated design prototype checks (not production E2E)."""

from pathlib import Path

import pytest

PROTOTYPE = Path(__file__).resolve().parents[1] / "prototypes" / "ux-00" / "index.html"


def test_prototype_network_isolation_contract() -> None:
    html = PROTOTYPE.read_text(encoding="utf-8")
    assert "connect-src 'none'" in html
    assert "form-action 'none'" in html
    assert "<script src=" not in html
    assert "<link rel=" not in html
    assert "Демонстрационный прототип" in html


@pytest.mark.parametrize("width", [390, 1440])
def test_prototype_role_and_moderation(width: int) -> None:
    from playwright.sync_api import Request, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors: list[str] = []
        requests: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def record_request(request: Request) -> None:
            if request.url.startswith(("http:", "https:")):
                requests.append(request.url)

        page.on("request", record_request)
        try:
            page.goto(PROTOTYPE.as_uri())
            page.get_by_role("button", name="Войти", exact=True).click()
            view = page.locator("#view")
            view.get_by_role("button", name="+ Создать пост", exact=True).click()
            page.locator("#post-text").fill("Проверка UX")
            page.locator('[data-target="tg"]').check()
            view.get_by_role("button", name="Предпросмотр").click()
            assert "Проверка UX" in page.locator("#dialog").inner_text()
            page.locator("#dialog").get_by_role("button", name="Закрыть").click()
            view.get_by_role("button", name="На проверку").click()
            assert "На проверке" in page.locator("#dialog").inner_text()
            page.locator("#dialog").get_by_role("button", name="Закрыть").click()
            page.locator("#role").select_option("admin")
            nav = page.locator("#nav" if width == 1440 else "#mobile")
            nav.get_by_role("button", name="Модерация").click()
            row = view.locator(".item").filter(has_text="Проверка UX")
            row.get_by_role("button", name="Детали: Проверка UX", exact=True).click()
            page.locator("#dialog").get_by_role("button", name="Одобрить").click()
            assert view.locator(".item").filter(has_text="Проверка UX").count() == 0
            page.locator("#theme").click()
            assert page.locator("html").get_attribute("data-theme") == "dark"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
            assert not requests
        finally:
            browser.close()


@pytest.mark.parametrize("width", [360, 768, 1440])
def test_prototype_r2_calendar_attention_and_layout(width: int) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        try:
            page.goto(PROTOTYPE.as_uri())
            page.get_by_role("button", name="Войти", exact=True).click()
            view = page.locator("#view")
            attention = view.locator('[data-action="metric:attention"]')
            assert attention.locator("b").inner_text() == "3"
            attention.click()
            assert "Instagram" in view.inner_text()
            assert "Нужен повторный вход" in view.inner_text()
            nav = page.locator("#mobile" if width <= 640 else "#nav")
            nav.get_by_role("button", name="Календарь", exact=True).click()
            assert view.locator(".week-grid .event").count() == 5
            assert "Открытая встреча" in view.locator(".week-grid").inner_text()
            view.locator("#cal-platform").select_option("ig")
            assert view.locator(".week-grid .event").count() == 2
            view.get_by_role("button", name="Месяц", exact=True).click()
            assert view.locator(".month-grid").count() == 1
            view.get_by_role("button", name="Неделя", exact=True).click()
            view.locator('[data-action="new-on:2026-11-27"]').click()
            assert view.locator("#date").input_value() == "2026-11-27"
            page.locator("#role").select_option("admin")
            page.locator("#profile").select_option("yoga")
            count = view.locator('[data-action="metric:attention"] b')
            assert count.inner_text() == "1"
            if width > 640:
                page.locator("#collapse").click()
                assert page.locator(".shell.collapsed").count() == 1
                page.locator("#collapse").click()
                assert page.locator(".shell.collapsed").count() == 0
            page.locator("#theme").click()
            assert page.locator("html").get_attribute("data-theme") == "dark"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1440])
def test_prototype_client_onboarding(width: int) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        try:
            page.goto(PROTOTYPE.as_uri())
            page.get_by_role("button", name="Войти", exact=True).click()
            view = page.locator("#view")
            page.locator("#role").select_option("admin")
            nav = page.locator("#nav" if width == 1440 else "#mobile")
            if width == 390:
                nav.get_by_role("button", name="Ещё").click()
                view.get_by_role("button", name="Клиенты", exact=True).click()
            else:
                nav.get_by_role("button", name="Клиенты", exact=True).click()
            view.get_by_role("button", name="+ Пригласить клиента").click()
            view.get_by_role("button", name="Создать приглашение (демо)").click()
            view.get_by_role("button", name="Открыть как клиент").click()
            view.get_by_role("button", name="Создать аккаунт (демо)").click()
            assert "Подключено 0 из 5" in view.inner_text()
            view.locator('[data-action="setup:tg"]').click()
            assert "App configuration" in view.inner_text()
            for _ in range(3):
                view.get_by_role("button", name="Далее", exact=True).click()
            view.get_by_role("button", name="Симулировать успешную проверку").click()
            assert "Подключено 1 из 5" in view.inner_text()
            view.get_by_role("button", name="Создать первую публикацию").click()
            assert view.locator("#post-text").count() == 1
            page.locator("#role").select_option("admin")
            page.locator("#role").select_option("client")
            assert "Новый клиент (демо)" in page.locator("#context-name").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()


def test_prototype_auth_entry_and_recovery() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 900})
        try:
            page.goto(PROTOTYPE.as_uri())
            assert page.locator(".auth-shell").count() == 1
            assert not page.locator("#mobile").is_visible()
            page.locator("#login-pass").fill("incorrect-demo")
            page.get_by_role("button", name="Войти", exact=True).click()
            assert "Не удалось войти" in page.get_by_role("alert").inner_text()
            page.get_by_role("button", name="Забыли пароль?").click()
            assert "владельцу Crosspost" in page.locator("#view").inner_text()
            page.get_by_role("button", name="Вернуться ко входу").click()
            page.get_by_role("button", name="Войти", exact=True).click()
            assert page.locator(".auth-shell").count() == 0
            assert page.locator("#mobile").is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()
