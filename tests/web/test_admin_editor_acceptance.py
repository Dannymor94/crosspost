"""Desktop editor acceptance with pinned React and synthetic API; no external publishing."""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

FRONTEND = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend"
ASSETS = os.environ.get("UX_BROWSER_ASSETS")


@pytest.mark.skipif(not ASSETS, reason="Run dedicated UX browser acceptance workflow")
@pytest.mark.parametrize(
    "scenario",
    [
        "unknown",
        "submitted",
        "incomplete",
        "rejected",
        "stale",
        "navigation",
        "calendar",
        "history",
        "connections",
    ],
)
def test_admin_draft_reload_and_unknown_utc_schedule(scenario):
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
    moves = 0
    calendar_when = "2099-01-15T22:30:00Z"
    dispatched = 0
    outcome = {"channel": "telegram", "target": "", "status": "submitted"}
    operations = {}
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
        nonlocal draft, writes, sends, resolves, recoveries, dispatched
        nonlocal moves, calendar_when
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
        elif path.startswith("/static/") and path.endswith((".js", ".css")):
            asset = FRONTEND / path.removeprefix("/static/")
            mime = "text/css" if asset.suffix == ".css" else "application/javascript"
            route.fulfill(path=asset, content_type=mime)
        elif path == "/api/auth/me":
            actor = {"id": 2, "login": "test-admin", "role": "admin", "profile_id": None}
            route.fulfill(json=actor)
        elif path == "/api/profiles":
            profiles = [{"id": 1, "name": "Демо-профиль"}]
            if scenario == "navigation":
                profiles.append({"id": 2, "name": "Второй профиль"})
            route.fulfill(json=profiles)
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
            vk = {
                **target,
                "channel": "vk_wall",
                "title": "VK",
                "platform": "vk",
                "platform_title": "VK",
            }
            route.fulfill(json=[target, vk] if scenario == "incomplete" else [target])
        elif path.endswith("/editor-draft"):
            assert request.headers.get("x-draft-actor") == "2"
            if request.method == "GET":
                route.fulfill(json=draft)
            else:
                if scenario == "navigation":
                    route.fulfill(status=503, json={"detail": "synthetic save failure"})
                    return
                body = request.post_data_json
                if body["scope"] != draft["scope"] or body["revision"] != draft["revision"]:
                    route.fulfill(status=409, json={"detail": "conflict"})
                    return
                if draft.get("operation_state") == "used" or (
                    draft.get("operation_state") == "recorded" and body["data"] is not None
                ):
                    route.fulfill(status=409, json={"detail": "consumed"})
                    return
                writes += 1
                revision = f"{writes:032x}"
                draft = {"scope": draft["scope"], "revision": revision, "data": body["data"]}
                route.fulfill(json=draft)
        elif path.endswith("/scheduled/resolve-time"):
            resolves += 1
            assert request.post_data_json == {"wall": wall_time, "zone": "Europe/Moscow"}
            route.fulfill(json={"instant": instant})
        elif scenario == "calendar" and path == "/api/profiles/1/scheduled/31/move":
            assert request.method == "POST"
            moves += 1
            body = request.post_data_json
            assert body["expected_at"] == calendar_when
            assert body["zone"] == "Europe/Moscow"
            if moves == 1:
                assert body["wall"] == "2099-01-16T02:30"
                calendar_when = "2099-01-15T23:30:00Z"
                route.fulfill(json={"id": 31, "scheduled_at": calendar_when, "status": "scheduled"})
            else:
                route.fulfill(status=409, json={"detail": "conflict"})
        elif scenario == "calendar" and path == "/api/profiles/1/scheduled":
            route.fulfill(
                json=[
                    {
                        "id": 31,
                        "status": "scheduled",
                        "text": "Календарный тест UTC",
                        "scheduled_at": calendar_when,
                        "time_basis": "utc",
                        "content_type": "post",
                        "title": None,
                        "channels": ["telegram"],
                        "media_paths": [],
                    },
                    {
                        "id": 32,
                        "status": "pending_approval",
                        "text": "Календарный тест legacy",
                        "scheduled_at": "2099-01-15T12:30:00",
                        "time_basis": "legacy_local",
                        "content_type": "post",
                        "title": None,
                        "channels": ["telegram"],
                        "media_paths": [],
                    },
                ]
            )
        elif scenario == "history" and path == "/api/profiles/1/history":
            if "before=" in parsed.query:
                entries = [
                    {
                        "source": "scheduled",
                        "key": "4",
                        "created_at_utc": "2026-11-27T09:00:00.000Z",
                        "text": "Отменённый пост",
                        "status": "cancelled",
                        "coverage": "observed_only",
                        "outcomes": [],
                    }
                ]
                cursor = None
            else:
                entries = [
                    {
                        "source": "immediate",
                        "key": "immediate-a",
                        "created_at_utc": "2026-11-27T10:00:00.000Z",
                        "text": None,
                        "status": "submitted_observed",
                        "coverage": "observed_only",
                        "outcomes": [{"channel": "telegram", "target": "", "status": "submitted"}],
                    }
                ]
                cursor = "next-page"
            route.fulfill(
                json={
                    "entries": entries,
                    "next_cursor": cursor,
                    "scope": "scheduled_and_observed_immediate",
                }
            )
        elif scenario == "connections" and path == "/api/profiles/1/channels":
            route.fulfill(
                json=[
                    {
                        "channel": "telegram",
                        "title": "Telegram",
                        "kind": "api",
                        "state": "live",
                        "has_credential": True,
                        "needs_target": False,
                        "guide": {"steps": [{"title": "Подготовьте аккаунт"}]},
                    },
                    {
                        "channel": "vk_wall",
                        "title": "ВКонтакте",
                        "kind": "browser",
                        "state": "banned",
                        "has_credential": True,
                        "needs_target": False,
                        "guide": {"steps": [{"title": "Вход в VK"}]},
                    },
                    {
                        "channel": "instagram",
                        "title": "Instagram",
                        "kind": "api",
                        "state": "unexpected",
                        "has_credential": True,
                        "needs_target": False,
                    },
                ]
            )
        elif path.endswith("/operation-status"):
            recoveries += 1
            assert request.headers.get("idempotency-key")
            assert request.headers.get("idempotency-recovery")
            if scenario == "unknown":
                route.fulfill(json={"state": "unknown", "outcomes": []})
            else:
                key = request.headers["idempotency-key"]
                proof = request.headers["idempotency-recovery"]
                operation = operations.get(key)
                if operation is None or operation[0] != proof:
                    route.fulfill(json={"state": "not_admitted", "outcomes": []})
                else:
                    route.fulfill(json=operation[1])
        elif path.endswith("/publish") and request.method == "POST":
            sends += 1
            key = request.headers["idempotency-key"]
            proof = request.headers["idempotency-recovery"]
            assert request.headers["x-draft-revision"] == key
            assert request.headers["x-draft-scope"] == draft["scope"]
            if key != draft["revision"] or key in operations:
                route.fulfill(status=409, json={"detail": "stale revision"})
                return
            if scenario == "rejected" and not operations:
                receipt = {
                    "state": "rejected",
                    "outcomes": [{"status": "rejected_before_effect", "http_status": "422"}],
                }
                operations[key] = (proof, receipt)
                draft["operation_state"] = "rejected"
                route.fulfill(status=422, json={"detail": "validation rejected"})
                return
            dispatched += 1
            status = "done" if scenario in ("stale", "rejected", "incomplete") else "submitted"
            rows = [{**outcome, "status": status}]
            operations[key] = (proof, {"state": "recorded", "outcomes": rows})
            draft["operation_state"] = "used" if scenario == "incomplete" else "recorded"
            route.fulfill(json={"publication_id": "p1", "outcomes": rows})
        elif path.endswith("/scheduled") and request.method == "POST":
            sends += 1
            assert request.headers.get("idempotency-key")
            assert request.headers.get("idempotency-recovery")
            assert request.headers.get("x-schedule-time-contract") == "utc-v1"
            assert instant in (request.post_data or "")
            route.abort("failed")
        elif scenario == "navigation" and path == "/api/profiles/1/scheduled":
            route.fulfill(
                json=[
                    {
                        "id": 11,
                        "status": "scheduled",
                        "text": "Сводка первого профиля",
                        "scheduled_at": "2099-01-15T09:30:00Z",
                        "time_basis": "utc",
                        "content_type": "post",
                        "title": None,
                        "channels": ["telegram"],
                        "media_paths": [],
                    }
                ]
            )
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
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            context.route("**/*", handle)
            page.goto("http://localhost/")
            if scenario == "unknown":
                theme = page.get_by_role("combobox", name="Тема оформления")
                theme.select_option("light")
                expect(page.locator("html")).to_have_attribute("data-theme", "light")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                capture(page, "admin-00-light-theme.png")
                theme.select_option("dark")
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                capture(page, "admin-00-dark-theme.png")
            text = page.get_by_placeholder("Текст поста…")
            if scenario == "connections":
                menu = page.get_by_role("navigation", name="Меню администратора", exact=True)
                menu.get_by_role("button", name="Подключения", exact=True).click()
                expect(page.get_by_text("Ограничено площадкой", exact=True)).to_be_visible()
                expect(page.get_by_text("Состояние неизвестно", exact=True)).to_be_visible()
                expect(page.get_by_text("Подключено", exact=True)).to_be_visible()
                blocked = page.locator(".channel-card").filter(has_text="ВКонтакте")
                expect(blocked.get_by_role("button")).to_have_count(0)
                unknown = page.locator(".channel-card").filter(has_text="Instagram")
                expect(unknown.get_by_role("button")).to_have_count(0)
                expect(page.get_by_role("button", name="Проверить", exact=True)).to_have_count(1)
                page.get_by_role("button", name="Обновить состояния").click()
                expect(page.get_by_text("Ограничено площадкой", exact=True)).to_be_visible()
                capture(page, "admin-07-connections.png")
                assert sends == 0
                assert not errors, errors
                return
            if scenario == "history":
                menu = page.get_by_role("navigation", name="Меню администратора", exact=True)
                menu.get_by_role("button", name="История", exact=True).click()
                expect(page.get_by_role("heading", name="История", exact=True)).to_be_visible()
                expect(page.get_by_text("Текст не был сохранён в истории")).to_be_visible()
                expect(
                    page.get_by_text("Отправлено без подтверждения", exact=True).first
                ).to_be_visible()
                expect(page.get_by_text("Опубликовано", exact=True)).to_have_count(0)
                page.get_by_role("button", name="Показать ещё").click()
                expect(page.get_by_text("Отменённый пост", exact=True)).to_be_visible()
                page.get_by_label("Тип публикации").select_option("scheduled")
                expect(page.get_by_text("Текст не был сохранён в истории")).to_have_count(0)
                page.get_by_label("Тип публикации").select_option("all")
                page.get_by_label("Поиск по загруженным записям").fill("Отменённый")
                expect(page.get_by_text("Отменённый пост", exact=True)).to_be_visible()
                expect(page.get_by_text("Текст не был сохранён в истории")).to_have_count(0)
                capture(page, "admin-06-history.png")
                assert sends == 0
                assert not errors, errors
                return
            if scenario == "calendar":
                menu = page.get_by_role("navigation", name="Меню администратора", exact=True)
                menu.get_by_role("button", name="Календарь", exact=True).click()
                expect(page.get_by_role("heading", name="Календарь", exact=True)).to_be_visible()
                expect(page.get_by_text("Календарный тест UTC", exact=True)).to_be_visible()
                expect(page.get_by_text("Календарный тест legacy", exact=True)).to_be_visible()
                expect(page.get_by_text("2099-01-16 01:30 · Europe/Moscow")).to_be_visible()
                expect(page.get_by_text("Записи без точной даты профиля")).to_be_visible()
                page.get_by_role("button", name="День", exact=True).click()
                page.get_by_label("Дата календаря").fill("2099-01-16")
                expect(page.get_by_text("Календарный тест UTC", exact=True)).to_be_visible()
                page.get_by_role("button", name="Неделя", exact=True).click()
                expect(page.locator(".cp-calendar-cell")).to_have_count(7)
                page.get_by_role("button", name="Месяц", exact=True).click()
                expect(page.locator(".cp-calendar-cell")).to_have_count(31)
                page.get_by_label("Статус", exact=True).select_option("pending_approval")
                expect(page.get_by_text("Календарный тест UTC", exact=True)).to_have_count(0)
                expect(page.get_by_text("Календарный тест legacy", exact=True)).to_be_visible()
                page.get_by_label("Статус", exact=True).select_option("all")
                capture(page, "admin-05-calendar.png")
                expect(page.get_by_role("button", name="Перенести", exact=True)).to_have_count(1)
                page.get_by_role("button", name="Перенести", exact=True).click()
                page.get_by_label("Новое время публикации").fill("2099-01-16T02:30")
                page.get_by_role("button", name="Подтвердить перенос").click()
                expect(page.get_by_text("Новое время сохранено.", exact=True)).to_be_visible()
                expect(page.get_by_text("2099-01-16 02:30 · Europe/Moscow")).to_be_visible()
                page.get_by_role("button", name="Перенести", exact=True).click()
                page.get_by_label("Новое время публикации").fill("2099-01-16T03:30")
                page.get_by_role("button", name="Подтвердить перенос").click()
                expect(page.get_by_text("Перенос не выполнен:", exact=False)).to_be_visible()
                expect(page.get_by_text("2099-01-16 02:30 · Europe/Moscow")).to_be_visible()
                assert moves == 2
                page.get_by_role("button", name="Управление расписанием").click()
                expect(text).to_be_visible()
                menu.get_by_role("button", name="Календарь", exact=True).click()
                page.get_by_role("button", name="День", exact=True).click()
                page.get_by_label("Дата календаря").fill("2099-01-17")
                page.get_by_label("Время нового поста").fill("13:45")
                page.get_by_role("button", name="Создать пост на выбранное время").click()
                expect(text).to_be_visible()
                expect(page.get_by_text("Выбрано в календаре:", exact=False)).to_be_visible()
                expect(
                    page.get_by_role("button", name="Запланировать на выбранное время")
                ).to_be_disabled()
                expect(page.get_by_text("Выбрано целей: 0 из 1", exact=True)).to_be_visible()
                page.get_by_role("button", name="Выбрать все доступные", exact=True).click()
                expect(page.get_by_text("Выбрано целей: 1 из 1", exact=True)).to_be_visible()
                page.get_by_role("button", name="Снять выбор", exact=True).click()
                expect(page.get_by_text("Выбрано целей: 0 из 1", exact=True)).to_be_visible()
                expect(page.get_by_text("Черновик сохранён", exact=True)).to_be_visible()
                assert draft["data"]["targets"] == []
                assert sends == 0
                assert not errors, errors
                return
            if scenario == "navigation":
                desktop = page.get_by_role("navigation", name="Меню администратора", exact=True)
                mobile = page.get_by_role("navigation", name="Мобильное меню администратора")
                expect(desktop).to_be_visible()
                expect(mobile).to_be_hidden()
                desktop.get_by_role("button", name="Обзор", exact=True).click()
                expect(page.locator(".cp-overview-row")).to_have_count(1)
                expect(page.get_by_text("Сводка первого профиля", exact=True)).to_be_visible()
                expect(page.locator(".cp-overview-metrics strong")).to_have_text(["1", "0"])
                for label in (
                    "Обзор",
                    "Импорт плана",
                    "Модерация",
                    "Клиенты",
                    "Подключения",
                    "Диагностика",
                ):
                    item = desktop.get_by_role("button", name=label, exact=True)
                    item.click()
                    expect(item).to_have_attribute("aria-current", "page")
                desktop.get_by_role("button", name="Публикации", exact=True).click()
                expect(text).to_be_visible()
                expect(page.get_by_text("Нет сохранённого черновика", exact=True)).to_be_visible()
                text.fill(draft_text)
                expect(page.get_by_text("Не удалось сохранить черновик.")).to_be_visible()
                page.once("dialog", lambda dialog: dialog.dismiss())
                desktop.get_by_role("button", name="Обзор", exact=True).click()
                expect(text).to_have_value(draft_text)
                expect(desktop.get_by_role("button", name="Публикации")).to_have_attribute(
                    "aria-current", "page"
                )
                profile = page.get_by_role("combobox", name="Активный профиль")
                page.once("dialog", lambda dialog: dialog.dismiss())
                profile.select_option("2")
                expect(profile).to_have_value("1")
                expect(text).to_have_value(draft_text)
                page.once("dialog", lambda dialog: dialog.dismiss())
                page.evaluate("history.back()")
                expect(text).to_have_value(draft_text)
                page.wait_for_function("history.state.crosspostWorkspace.view === 'publish'")
                page.once("dialog", lambda dialog: dialog.accept())
                desktop.get_by_role("button", name="Обзор", exact=True).click()
                expect(page.get_by_role("heading", name="Обзор", exact=True)).to_be_visible()
                page.evaluate("history.back()")
                expect(text).to_have_value("")
                page.evaluate("history.forward()")
                expect(page.get_by_role("heading", name="Обзор", exact=True)).to_be_visible()
                profile.select_option("2")
                expect(profile).to_have_value("2")
                expect(text).to_have_value("")
                page.evaluate("history.back()")
                expect(profile).to_have_value("2")
                expect(text).to_have_value("")
                desktop.get_by_role("button", name="Обзор", exact=True).click()
                expect(page.get_by_role("heading", name="Обзор", exact=True)).to_be_visible()
                expect(page.get_by_text("Активных публикаций нет.")).to_be_visible()
                expect(page.get_by_text("Сводка первого профиля", exact=True)).to_have_count(0)
                expect(page.locator(".cp-overview-metrics strong")).to_have_text(["0", "0"])
                for width in (360, 390, 768, 1440):
                    page.set_viewport_size({"width": width, "height": 900})
                    expect(mobile if width <= 640 else desktop).to_be_visible()
                    expect(desktop if width <= 640 else mobile).to_be_hidden()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.set_viewport_size({"width": 390, "height": 844})
                mobile.get_by_role("button", name="Ещё", exact=True).click()
                expect(page.get_by_role("heading", name="Ещё", exact=True)).to_be_visible()
                page.get_by_role("button", name="Клиенты", exact=True).click()
                expect(page.get_by_role("button", name="Новый профиль")).to_be_visible()
                expect(page.get_by_role("button", name="Аккаунт клиента")).to_be_visible()
                expect(mobile.get_by_role("button", name="Ещё")).to_have_attribute(
                    "aria-current", "page"
                )
                capture(page, "admin-04-mobile-clients.png")
                assert sends == 0
                assert not errors, errors
                return
            text.fill(draft_text)
            target = page.locator("button.target").filter(has_text="Telegram")
            expect(target).to_have_attribute("aria-pressed", "false")
            target.click()
            if scenario != "unknown":
                if scenario == "incomplete":
                    page.locator("button.target").filter(has_text="VK").click()
                send = page.get_by_role("button", name="Опубликовать сейчас", exact=True)
                expect(send).to_be_enabled(timeout=10000)
                original_revision = draft["revision"]
                if scenario == "stale":
                    second = context.new_page()
                    second.on("pageerror", lambda error: errors.append(str(error)))
                    second.goto("http://localhost/")
                    expect(second.get_by_placeholder("Текст поста…")).to_have_value(draft_text)
                    second_send = second.get_by_role(
                        "button", name="Опубликовать сейчас", exact=True
                    )
                    expect(second_send).to_be_enabled(timeout=10000)
                    send.click()
                    expect(text).to_have_value("")
                    assert draft["data"] is None
                    assert draft["revision"] != original_revision
                    second_send.click()
                    expect(second_send).to_be_disabled()
                    second.get_by_role("button", name="Проверить результат").click()
                    expect(
                        second.get_by_text("Эта попытка не была допущена.", exact=False)
                    ).to_be_visible()
                    second.get_by_role("button", name="Результат проверен").click()
                    expect(second.get_by_placeholder("Текст поста…")).to_have_value("")
                    assert sends == 2 and dispatched == 1
                else:
                    send.click()
                    expect(send).to_be_disabled()
                    expect(page.get_by_role("button", name="Проверить результат")).to_be_visible()
                    page.reload()
                    expect(text).to_have_value(draft_text)
                    expect(send).to_be_disabled()
                    page.get_by_role("button", name="Проверить результат").click()
                    acknowledge = page.get_by_role("button", name="Результат проверен")
                    if scenario == "incomplete":
                        expect(
                            page.get_by_text("Результат неизвестен.", exact=False)
                        ).to_be_visible()
                        expect(acknowledge).to_have_count(0)
                        assert draft["revision"] == original_revision
                        assert sends == dispatched == 1
                    else:
                        expect(acknowledge).to_be_visible()
                        acknowledge.click()
                        if scenario == "submitted":
                            expect(send).to_be_disabled()
                            expect(
                                page.get_by_role("button", name="Новый пост", exact=True)
                            ).to_be_visible()
                            assert draft["revision"] == original_revision
                            assert sends == dispatched == 1
                        else:
                            page.get_by_role("button", name="Продолжить редактирование").click()
                            expect(send).to_be_enabled(timeout=10000)
                            assert draft["revision"] != original_revision
                            assert draft["data"]["text"] == draft_text
                            assert sends == 1 and dispatched == 0
                            send.click()
                            expect(text).to_have_value("")
                            assert sends == 2 and dispatched == 1

                assert not errors, errors
                return
            schedule = page.get_by_role("button", name="Запланировать", exact=True)
            expect(schedule).to_be_enabled(timeout=10000)
            assert draft["data"]["targets"] == ["telegram␟"]
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
        except Exception:
            capture(page, "admin-failure.png")
            raise
        finally:
            browser.close()
