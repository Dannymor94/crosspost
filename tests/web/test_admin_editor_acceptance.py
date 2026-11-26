"""Desktop editor acceptance with pinned React and synthetic API; no external publishing."""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

FRONTEND = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend"
ASSETS = os.environ.get("UX_BROWSER_ASSETS")


@pytest.mark.skipif(not ASSETS, reason="Run dedicated UX browser acceptance workflow")
def test_admin_draft_reload_and_unknown_utc_schedule():
    assert ASSETS is not None
    assets = Path(ASSETS)
    react = assets / "react/umd/react.production.min.js"
    react_dom = assets / "react-dom/umd/react-dom.production.min.js"
    assert react.is_file() and react_dom.is_file()
    html = (FRONTEND / "index.html").read_text()
    # Substitute only dependency loading: application JS and DOM are unchanged.
    react_import = 'const React = await import("https://esm.sh/react@18");'
    dom_import = 'const { createRoot } = await import("https://esm.sh/react-dom@18/client");'
    assert react_import in html and dom_import in html
    html = html.replace(
        '<script type="module">',
        '<script src="/test-assets/react.js"></script>\n'
        '<script src="/test-assets/react-dom.js"></script>\n<script type="module">',
        1,
    )
    html = html.replace(react_import, "const React = window.React;")
    html = html.replace(dom_import, "const { createRoot } = window.ReactDOM;")
    draft = {"scope": "b" * 64, "revision": None, "data": None}
    writes = 0
    sends = 0
    resolves = 0
    recoveries = 0
    errors = []
    draft_text = "Тестовый черновик администратора"
    wall_time = "2099-01-15T12:30"
    instant = "2099-01-15T09:30:00+00:00"

    def capture(page, name):
        root = os.environ.get("UX_SCREENSHOTS")
        if root:
            directory = Path(root)
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=directory / name, full_page=True)

    def handle(route):
        nonlocal draft, writes, sends, resolves, recoveries
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if parsed.hostname != "localhost":
            route.abort()
        elif path == "/":
            route.fulfill(body=html, content_type="text/html")
        elif path == "/test-assets/react.js":
            route.fulfill(path=react, content_type="application/javascript")
        elif path == "/test-assets/react-dom.js":
            route.fulfill(path=react_dom, content_type="application/javascript")
        elif path.startswith("/static/") and path.endswith(".js"):
            script = FRONTEND / Path(path).name
            route.fulfill(path=script, content_type="application/javascript")
        elif path == "/api/auth/me":
            actor = {"id": 2, "login": "test-admin", "role": "admin", "profile_id": None}
            route.fulfill(json=actor)
        elif path == "/api/profiles":
            route.fulfill(json=[{"id": 1, "name": "Демо-профиль"}])
        elif path == "/api/settings/browser":
            route.fulfill(json={"headless": True})
        elif path.endswith("/settings/timezone"):
            route.fulfill(json={"zone": "Europe/Moscow"})
        elif path.endswith("/publish/targets"):
            target = {
                "channel": "telegram",
                "target_id": "",
                "eligible": True,
                "title": "Telegram",
                "platform": "telegram",
                "platform_title": "Telegram",
                "state": "live",
            }
            route.fulfill(json=[target])
        elif path.endswith("/editor-draft"):
            assert request.headers.get("x-draft-actor") == "2"
            if request.method == "GET":
                route.fulfill(json=draft)
            else:
                body = request.post_data_json
                if body["scope"] != draft["scope"] or body["revision"] != draft["revision"]:
                    route.fulfill(status=409, json={"detail": "conflict"})
                    return
                writes += 1
                revision = f"{writes:032x}"
                draft = {"scope": draft["scope"], "revision": revision, "data": body["data"]}
                route.fulfill(json=draft)
        elif path.endswith("/scheduled/resolve-time"):
            resolves += 1
            assert request.post_data_json == {"wall": wall_time, "zone": "Europe/Moscow"}
            route.fulfill(json={"instant": instant})
        elif path.endswith("/operation-status"):
            recoveries += 1
            assert request.headers.get("idempotency-key")
            assert request.headers.get("idempotency-recovery")
            route.fulfill(json={"state": "unknown", "outcomes": []})
        elif path.endswith("/scheduled") and request.method == "POST":
            sends += 1
            assert request.headers.get("idempotency-key")
            assert request.headers.get("idempotency-recovery")
            assert request.headers.get("x-schedule-time-contract") == "utc-v1"
            assert instant in (request.post_data or "")
            route.abort("failed")
        elif request.method == "GET" and any(
            path.endswith(suffix)
            for suffix in ("/scheduled", "/scheduled/pending", "/scheduled/report")
        ):
            route.fulfill(json=[])
        else:
            route.fulfill(status=404, json={"detail": "unexpected mock route"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", handle)
            page.goto("http://localhost/")
            text = page.get_by_placeholder("Текст поста…")
            text.fill(draft_text)
            target = page.locator("button.target").filter(has_text="Telegram")
            expect(target).to_have_attribute("aria-pressed", "false")
            target.click()
            schedule = page.get_by_role("button", name="Запланировать", exact=True)
            expect(schedule).to_be_enabled(timeout=10000)
            assert draft["data"]["targets"] == ["telegram"]
            capture(page, "admin-01-editor.png")
            page.reload()
            expect(text).to_have_value(draft_text)
            expect(target).to_have_attribute("aria-pressed", "true")
            expect(schedule).to_be_enabled(timeout=10000)
            schedule.click()
            time_input = page.locator("#sched-when")
            expect(time_input).to_be_enabled()
            time_input.fill(wall_time)
            capture(page, "admin-02-schedule.png")
            page.get_by_role("button", name="Сохранить в план").click()
            warning = page.get_by_text("Повторная отправка заблокирована.", exact=False)
            expect(warning).to_be_visible(timeout=10000)
            assert sends == 1 and resolves == 1
            assert draft["data"]["text"] == draft_text
            page.reload()
            expect(text).to_have_value(draft_text)
            expect(warning).to_be_visible()
            expect(schedule).to_be_disabled()
            page.get_by_role("button", name="Проверить результат").click()
            expect(page.get_by_text("Результат неизвестен.", exact=False)).to_be_visible()
            expect(schedule).to_be_disabled()
            assert sends == 1 and recoveries == 1
            capture(page, "admin-03-repeat-blocked.png")
            assert not errors, errors
        finally:
            browser.close()
