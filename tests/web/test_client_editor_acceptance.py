"""Actual client page with synthetic HTTP responses; never contact a social platform."""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

FRONTEND = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend"
ASSETS = os.environ.get("UX_BROWSER_ASSETS")


@pytest.mark.skipif(not ASSETS, reason="Run dedicated UX browser acceptance workflow")
def test_client_draft_reload_media_loss_and_unknown_send():
    assert ASSETS is not None
    assets = Path(ASSETS)
    scripts = {
        "/preact@10.24.3/dist/preact.umd.js": assets / "preact/dist/preact.umd.js",
        "/preact@10.24.3/hooks/dist/hooks.umd.js": assets / "preact/hooks/dist/hooks.umd.js",
        "/htm@3.1.1/dist/htm.js": assets / "htm/dist/htm.js",
    }
    assert all(path.is_file() for path in scripts.values())
    draft = {"scope": "a" * 64, "revision": None, "data": None}
    writes = 0
    sends = 0
    errors = []

    def handle(route):
        nonlocal draft, writes, sends
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if parsed.hostname == "unpkg.com" and path in scripts:
            route.fulfill(path=scripts[path], content_type="application/javascript")
        elif parsed.hostname != "localhost":
            route.abort()
        elif path == "/client":
            route.fulfill(path=FRONTEND / "client.html", content_type="text/html")
        elif path.startswith("/static/") and path.endswith(".js"):
            route.fulfill(
                path=FRONTEND / Path(path).name, content_type="application/javascript"
            )
        elif path == "/api/auth/me":
            route.fulfill(json={"id": 1, "login": "test-client", "role": "client", "profile_id": 1})
        elif path.endswith("/settings/moderation"):
            route.fulfill(json={"enabled": False})
        elif path.endswith("/settings/timezone"):
            route.fulfill(json={"zone": "Europe/Moscow"})
        elif path.endswith("/publish/targets"):
            route.fulfill(
                json=[
                    {"channel": "telegram", "target_id": "", "eligible": True, "title": "Telegram"},
                    {"channel": "vk", "target_id": "", "eligible": True, "title": "VK"},
                ]
            )
        elif path.endswith("/editor-draft"):
            assert request.headers.get("x-draft-actor") == "1"
            if request.method == "GET":
                route.fulfill(json=draft)
            else:
                body = request.post_data_json
                if body["scope"] != draft["scope"] or body["revision"] != draft["revision"]:
                    route.fulfill(status=409, json={"detail": "conflict"})
                    return
                writes += 1
                draft = {"scope": draft["scope"], "revision": f"{writes:032x}", "data": body["data"]}
                route.fulfill(json=draft)
        elif path.endswith("/publish"):
            sends += 1
            assert request.headers.get("idempotency-key")
            assert request.headers.get("idempotency-recovery")
            route.abort("failed")
        else:
            route.fulfill(status=404, json={"detail": "unexpected mock route"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", handle)
            page.goto("http://localhost/client")
            text = page.get_by_placeholder("Напишите текст поста…")
            text.fill("Synthetic draft")
            telegram = page.get_by_role("checkbox", name="Telegram")
            vk = page.get_by_role("checkbox", name="VK")
            expect(telegram).not_to_be_checked()
            telegram.check()
            page.locator('input[type="file"]').set_input_files(
                {"name": "test.png", "mimeType": "image/png", "buffer": b"synthetic"}
            )
            expect(page.get_by_text("Черновик сохранён", exact=True)).to_be_visible()
            expect(vk).not_to_be_checked()
            assert draft["data"]["media_required"] is True
            assert draft["data"]["targets"] == ["telegram"]
            page.reload()
            expect(text).to_have_value("Synthetic draft")
            expect(telegram).to_be_checked()
            expect(vk).not_to_be_checked()
            expect(page.get_by_text("Медиа не восстановлено.", exact=False)).to_be_visible()
            send = page.get_by_role("button", name="Отправить", exact=True)
            expect(send).to_be_disabled()
            page.get_by_role("button", name="Без вложения").click()
            expect(send).to_be_enabled(timeout=10000)
            send.click()
            expect(page.get_by_text("Повторная отправка заблокирована.", exact=False)).to_be_visible()
            assert sends == 1
            assert draft["data"]["text"] == "Synthetic draft"
            page.reload()
            expect(text).to_have_value("Synthetic draft")
            expect(send).to_be_disabled()
            expect(page.get_by_text("Повторная отправка заблокирована.", exact=False)).to_be_visible()
            assert sends == 1
            assert not errors, errors
        finally:
            browser.close()
