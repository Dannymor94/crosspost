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
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors: list[str] = []
        requests: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: requests.append(request.url)
            if request.url.startswith(("http:", "https:"))
            else None,
        )
        try:
            page.goto(PROTOTYPE.as_uri())
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
            page.locator("#nav" if width == 1440 else "#mobile").get_by_role(
                "button", name="Модерация"
            ).click()
            row = view.locator(".item").filter(has_text="Проверка UX")
            row.get_by_role("button", name="Детали").click()
            page.locator("#dialog").get_by_role("button", name="Одобрить").click()
            assert view.locator(".item").filter(has_text="Проверка UX").count() == 0
            page.locator("#theme").click()
            assert page.locator("html").get_attribute("data-theme") == "dark"
            assert page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            assert not errors
            assert not requests
        finally:
            browser.close()
