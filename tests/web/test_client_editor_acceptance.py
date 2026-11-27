"""Actual client page with synthetic HTTP responses; never contact a social platform."""

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
        "help",
    ],
)
def test_client_draft_reload_media_loss_and_unknown_send(scenario):
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
    moves = 0
    calendar_when = "2099-01-15T22:30:00Z"
    dispatched = 0
    outcome = {"channel": "telegram", "target": "", "status": "submitted"}
    operations = {}
    errors = []
    draft_text = "Тестовый черновик — публикация не отправляется на реальные площадки."
    screenshots = Path(os.environ.get("UX_SCREENSHOTS", ""))
    if os.environ.get("UX_SCREENSHOTS"):
        screenshots.mkdir(parents=True, exist_ok=True)

    def capture(page, name):
        if os.environ.get("UX_SCREENSHOTS"):
            page.screenshot(path=screenshots / name, full_page=True)

    def handle(route):
        nonlocal draft, writes, sends, dispatched
        nonlocal moves, calendar_when
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if parsed.hostname == "unpkg.com" and path in scripts:
            route.fulfill(path=scripts[path], content_type="application/javascript")
        elif parsed.hostname != "localhost":
            route.abort()
        elif path == "/client":
            route.fulfill(path=FRONTEND / "client.html", content_type="text/html")
        elif path.startswith("/static/") and path.endswith((".js", ".css")):
            asset = FRONTEND / path.removeprefix("/static/")
            mime = "text/css" if asset.suffix == ".css" else "application/javascript"
            route.fulfill(path=asset, content_type=mime)
        elif path == "/api/auth/me":
            actor = {"id": 1, "login": "test-client", "role": "client", "profile_id": 1}
            route.fulfill(json=actor)
        elif path.endswith("/settings/moderation"):
            route.fulfill(json={"enabled": False})
        elif path.endswith("/settings/timezone"):
            route.fulfill(json={"zone": "Europe/Moscow"})
        elif path.endswith("/publish/targets"):
            telegram = {
                "channel": "telegram",
                "target_id": "",
                "eligible": True,
                "title": "Telegram",
            }
            vk = {"channel": "vk", "target_id": "", "eligible": True, "title": "VK"}
            route.fulfill(json=[telegram, vk])
        elif path.endswith("/editor-draft"):
            assert request.headers.get("x-draft-actor") == "1"
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
        elif scenario == "history" and path == "/api/profiles/1/history/search":
            assert request.method == "POST"
            assert parsed.query == ""
            body = request.post_data_json
            assert body["before"] is None
            assert body["source"] in ("scheduled", "all")
            assert body["query"] in ("", "Отменённый")
            route.fulfill(
                json={
                    "entries": [
                        {
                            "source": "scheduled",
                            "key": "4",
                            "created_at_utc": "2026-11-27T09:00:00.000Z",
                            "text": "Отменённый пост",
                            "status": "cancelled",
                            "coverage": "observed_only",
                            "outcomes": [],
                        }
                    ],
                    "next_cursor": None,
                    "scope": "scheduled_and_observed_immediate",
                }
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
            key = request.headers["idempotency-key"]
            proof = request.headers["idempotency-recovery"]
            operation = operations.get(key)
            if operation is None or operation[0] != proof:
                route.fulfill(json={"state": "not_admitted", "outcomes": []})
            else:
                route.fulfill(json=operation[1])
        elif path.endswith("/publish"):
            if scenario == "unknown":
                sends += 1
                assert request.headers.get("idempotency-key")
                assert request.headers.get("idempotency-recovery")
                route.abort("failed")
            else:
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
        else:
            route.fulfill(status=404, json={"detail": "unexpected mock route"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            context.route("**/*", handle)
            page.goto("http://localhost/client")
            if scenario == "unknown":
                theme = page.get_by_role("combobox", name="Тема оформления")
                theme.select_option("light")
                expect(page.locator("html")).to_have_attribute("data-theme", "light")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                capture(page, "00-client-light-theme.png")
                theme.select_option("dark")
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                capture(page, "00-client-dark-theme.png")
            text = page.get_by_placeholder("Напишите текст поста…")
            if scenario == "help":
                menu = page.get_by_role("navigation", name="Мобильное меню клиента")
                menu.get_by_role("button", name="Главная", exact=True).click()
                page.get_by_role("button", name="Как пользоваться сервисом").click()
                expect(
                    page.get_by_role("heading", name="Как пользоваться Crosspost")
                ).to_be_visible()
                expect(page.get_by_text("5. Если потеряли доступ", exact=True)).to_be_visible()
                page.get_by_role("button", name="Открыть подключения").click()
                expect(page.get_by_role("heading", name="Подключения", exact=True)).to_be_visible()
                menu.get_by_role("button", name="Ещё", exact=True).click()
                page.get_by_role("button", name="Инструкция", exact=True).click()
                page.get_by_role("button", name="Открыть календарь").click()
                expect(page.get_by_role("heading", name="Календарь", exact=True)).to_be_visible()
                menu.get_by_role("button", name="Ещё", exact=True).click()
                page.get_by_role("button", name="Инструкция", exact=True).click()
                capture(page, "08-client-help.png")
                page.get_by_role("button", name="Создать пост", exact=True).click()
                expect(text).to_be_visible()
                assert sends == 0
                assert not errors, errors
                return
            if scenario == "connections":
                menu = page.get_by_role("navigation", name="Мобильное меню клиента")
                menu.get_by_role("button", name="Ещё", exact=True).click()
                page.get_by_role("button", name="Подключения", exact=True).click()
                expect(page.get_by_text("Ограничено площадкой", exact=True)).to_be_visible()
                expect(page.get_by_text("Состояние неизвестно", exact=True)).to_be_visible()
                expect(page.get_by_text("Подключено", exact=True)).to_be_visible()
                page.get_by_text("Этапы подключения", exact=True).first.click()
                expect(page.get_by_text("Подготовьте аккаунт", exact=True)).to_be_visible()
                page.get_by_label("Показать площадки").select_option("attention")
                expect(page.get_by_text("Подключено", exact=True)).to_have_count(0)
                expect(page.get_by_text("Ограничено площадкой", exact=True)).to_be_visible()
                page.get_by_label("Показать площадки").select_option("all")
                expect(page.locator("input[type=password]")).to_have_count(0)
                expect(page.get_by_role("button", name="Переподключить")).to_have_count(0)
                page.get_by_role("button", name="Обновить состояния").click()
                expect(page.get_by_text("Ограничено площадкой", exact=True)).to_be_visible()
                capture(page, "07-client-connections.png")
                assert sends == 0
                assert not errors, errors
                return
            if scenario == "history":
                menu = page.get_by_role("navigation", name="Мобильное меню клиента")
                menu.get_by_role("button", name="Главная", exact=True).click()
                expect(page.get_by_text("Записей с предупреждениями: 1")).to_be_visible()
                expect(page.get_by_text("telegram — Отправлено без подтверждения")).to_be_visible()
                page.get_by_role("button", name="К истории публикаций").click()
                expect(page.get_by_role("heading", name="История", exact=True)).to_be_visible()
                expect(page.get_by_text("Текст не был сохранён в истории")).to_be_visible()
                expect(
                    page.get_by_text("Отправлено без подтверждения", exact=True).first
                ).to_be_visible()
                expect(page.get_by_text("Опубликовано", exact=True)).to_have_count(0)
                page.get_by_role("button", name="Показать ещё").click()
                expect(page.get_by_text("Отменённый пост", exact=True)).to_be_visible()
                page.get_by_label("Тип публикации").select_option("scheduled")
                expect(page.get_by_text("Отменённый пост", exact=True)).to_be_visible()
                expect(page.get_by_text("Текст не был сохранён в истории")).to_have_count(0)
                page.get_by_label("Тип публикации").select_option("all")
                expect(page.get_by_text("Текст не был сохранён в истории")).to_be_visible()
                page.get_by_label("Поиск по сохранённым записям").fill("Отменённый")
                with page.expect_response(
                    lambda response: response.url.endswith("/history/search")
                ) as search:
                    page.get_by_role("button", name="Найти", exact=True).click()
                assert search.value.request.post_data_json["query"] == "Отменённый"
                expect(page.get_by_text("Отменённый пост", exact=True)).to_be_visible()
                expect(page.get_by_text("Текст не был сохранён в истории")).to_have_count(0)
                capture(page, "06-client-history.png")
                page.get_by_role("button", name="Использовать текст", exact=True).click()
                expect(text).to_have_value("")
                page.get_by_role("button", name="Вставить в пустой черновик").click()
                expect(text).to_have_value("Отменённый пост")
                expect(page.get_by_text("Черновик сохранён", exact=True)).to_be_visible()
                assert draft["data"]["targets"] == []
                assert draft["data"]["media_required"] is False
                menu.get_by_role("button", name="Ещё", exact=True).click()
                page.get_by_role("button", name="История", exact=True).click()
                page.get_by_role("button", name="Показать ещё").click()
                page.get_by_role("button", name="Использовать текст", exact=True).click()
                expect(text).to_have_value("Отменённый пост")
                expect(
                    page.get_by_role("button", name="Вставить в пустой черновик")
                ).to_be_disabled()
                assert sends == 0
                assert not errors, errors
                return
            if scenario == "calendar":
                menu = page.get_by_role("navigation", name="Мобильное меню клиента")
                menu.get_by_role("button", name="Расписание", exact=True).click()
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
                capture(page, "05-client-calendar.png")
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
                expect(page.get_by_role("button", name="К календарю")).to_be_visible()
                expect(page.get_by_text("Календарный тест UTC", exact=True)).to_be_visible()
                page.get_by_role("button", name="К календарю").click()
                page.get_by_role("button", name="День", exact=True).click()
                page.get_by_label("Дата календаря").fill("2099-01-17")
                page.get_by_label("Время нового поста").fill("13:45")
                page.get_by_role("button", name="Создать пост на выбранное время").click()
                expect(text).to_be_visible()
                expect(page.get_by_text("Выбрано в календаре:", exact=False)).to_be_visible()
                page.get_by_role("button", name="Использовать выбранное время").click()
                expect(page.locator("input[type=datetime-local]")).to_have_value("2099-01-17T13:45")
                expect(page.get_by_text("Выбрано целей: 0 из 2", exact=True)).to_be_visible()
                page.get_by_role("button", name="Выбрать все доступные", exact=True).click()
                expect(page.get_by_text("Выбрано целей: 2 из 2", exact=True)).to_be_visible()
                expect(page.get_by_text("Черновик сохранён", exact=True)).to_be_visible()
                assert len(draft["data"]["targets"]) == 2
                page.get_by_role("button", name="Снять выбор", exact=True).click()
                expect(page.get_by_text("Выбрано целей: 0 из 2", exact=True)).to_be_visible()
                expect(page.get_by_text("Черновик сохранён", exact=True)).to_be_visible()
                assert draft["data"]["targets"] == []
                assert sends == 0
                assert not errors, errors
                return
            if scenario == "navigation":
                desktop = page.get_by_role("navigation", name="Меню клиента", exact=True)
                mobile = page.get_by_role("navigation", name="Мобильное меню клиента")
                expect(mobile).to_be_visible()
                expect(desktop).to_be_hidden()
                expect(page.get_by_text("Нет сохранённого черновика", exact=True)).to_be_visible()
                text.fill(draft_text)
                expect(page.get_by_text("Не удалось сохранить черновик.")).to_be_visible()
                page.once("dialog", lambda dialog: dialog.dismiss())
                mobile.get_by_role("button", name="Главная", exact=True).click()
                expect(text).to_have_value(draft_text)
                expect(mobile.get_by_role("button", name="Новый пост")).to_have_attribute(
                    "aria-current", "page"
                )
                page.once("dialog", lambda dialog: dialog.accept())
                mobile.get_by_role("button", name="Главная", exact=True).click()
                expect(page.get_by_role("heading", name="Главная", exact=True)).to_be_visible()
                expect(page.get_by_text("Не удалось загрузить сводку.")).to_be_visible()
                expect(page.locator(".cp-overview-metrics")).to_have_count(0)
                page.get_by_role("button", name="Повторить загрузку").click()
                expect(page.get_by_text("Не удалось загрузить сводку.")).to_be_visible()
                capture(page, "04-client-home.png")
                page.evaluate("history.back()")
                expect(text).to_have_value("")
                page.evaluate("history.forward()")
                expect(page.get_by_role("heading", name="Главная", exact=True)).to_be_visible()
                page.get_by_role("button", name="Открыть расписание").click()
                expect(page.get_by_text("Не удалось загрузить расписание.")).to_be_visible()
                mobile.get_by_role("button", name="Ещё", exact=True).click()
                expect(page.get_by_role("button", name="Выйти из аккаунта")).to_be_visible()
                for width in (360, 390, 768, 1440):
                    page.set_viewport_size({"width": width, "height": 900})
                    expect(mobile if width <= 640 else desktop).to_be_visible()
                    expect(desktop if width <= 640 else mobile).to_be_hidden()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                desktop.get_by_role("button", name="Новый пост", exact=True).click()
                expect(text).to_have_value("")
                expect(page.get_by_text("Нет сохранённого черновика", exact=True)).to_be_visible()
                text.fill(draft_text)
                expect(page.get_by_text("Не удалось сохранить черновик.")).to_be_visible()
                page.once("dialog", lambda dialog: dialog.dismiss())
                page.evaluate("history.back()")
                expect(text).to_have_value(draft_text)
                page.wait_for_function("history.state.crosspostWorkspace.view === 'compose'")
                expect(desktop.get_by_role("button", name="Новый пост")).to_have_attribute(
                    "aria-current", "page"
                )
                assert sends == 0
                assert not errors, errors
                return
            text.fill(draft_text)
            telegram = page.get_by_role("checkbox", name="Telegram")
            vk = page.get_by_role("checkbox", name="VK")
            expect(telegram).not_to_be_checked()
            telegram.check()
            if scenario != "unknown":
                if scenario == "incomplete":
                    vk.check()
                send = page.get_by_role("button", name="Отправить", exact=True)
                expect(send).to_be_enabled(timeout=10000)
                original_revision = draft["revision"]
                if scenario == "stale":
                    second = context.new_page()
                    second.on("pageerror", lambda error: errors.append(str(error)))
                    second.goto("http://localhost/client")
                    expect(second.get_by_placeholder("Напишите текст поста…")).to_have_value(
                        draft_text
                    )
                    second_send = second.get_by_role("button", name="Отправить", exact=True)
                    expect(second_send).to_be_enabled(timeout=10000)
                    send.click()
                    expect(
                        page.get_by_text("Готово! Пост опубликован.", exact=False)
                    ).to_be_visible()
                    assert draft["data"] is None
                    assert draft["revision"] != original_revision
                    second_send.click()
                    expect(second_send).to_be_disabled()
                    second.get_by_role("button", name="Проверить результат").click()
                    expect(
                        second.get_by_text("Эта попытка не была допущена.", exact=False)
                    ).to_be_visible()
                    second.get_by_role("button", name="Результат проверен").click()
                    expect(second.get_by_placeholder("Напишите текст поста…")).to_have_value("")
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
                            expect(
                                page.get_by_text("Готово! Пост опубликован.", exact=False)
                            ).to_be_visible()
                            assert sends == 2 and dispatched == 1

                assert not errors, errors
                return
            expect(page.get_by_text("Черновик сохранён", exact=True)).to_be_visible()
            capture(page, "01-client-editor.png")
            upload = {"name": "test.png", "mimeType": "image/png", "buffer": b"synthetic"}
            page.locator('input[type="file"]').set_input_files(upload)
            expect(page.get_by_text("Черновик сохранён", exact=True)).to_be_visible()
            expect(vk).not_to_be_checked()
            assert draft["data"]["media_required"] is True
            assert draft["data"]["targets"] == ["telegram"]
            page.reload()
            expect(text).to_have_value(draft_text)
            expect(telegram).to_be_checked()
            expect(vk).not_to_be_checked()
            expect(page.get_by_text("Медиа не восстановлено.", exact=False)).to_be_visible()
            send = page.get_by_role("button", name="Отправить", exact=True)
            expect(send).to_be_disabled()
            capture(page, "02-draft-restored.png")
            page.get_by_role("button", name="Без вложения").click()
            expect(send).to_be_enabled(timeout=10000)
            send.click()
            expect(
                page.get_by_text("Повторная отправка заблокирована.", exact=False)
            ).to_be_visible()
            assert sends == 1
            assert draft["data"]["text"] == draft_text
            page.reload()
            expect(text).to_have_value(draft_text)
            expect(send).to_be_disabled()
            expect(
                page.get_by_text("Повторная отправка заблокирована.", exact=False)
            ).to_be_visible()
            assert sends == 1
            capture(page, "03-repeat-blocked.png")
            assert not errors, errors
        finally:
            browser.close()
