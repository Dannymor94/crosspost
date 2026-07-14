"""Тесты base_browser: storageState как источник сессии (без реального браузера).

async_playwright() мокается целиком. Проверяем:
  - open_page (publish): storageState грузится в new_context, если файл есть;
    и storage_state=None, если файла нет;
  - login_context (login): persistent-контекст + save_state() экспортирует
    storageState в storage_state_path(channel).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crosspost.adapters.browser import base_browser as bb


class _FakeAsyncPW:
    """async context manager, возвращающий заранее собранный pw."""

    def __init__(self, pw):
        self._pw = pw

    async def __aenter__(self):
        return self._pw

    async def __aexit__(self, *exc):
        return False


def _fake_playwright_for_publish():
    """pw для open_page: launch → browser.new_context → context.new_page."""
    pw = MagicMock()
    browser = AsyncMock()
    context = AsyncMock()
    page = AsyncMock()
    pw.chromium.launch = AsyncMock(return_value=browser)
    browser.new_context = AsyncMock(return_value=context)
    context.new_page = AsyncMock(return_value=page)
    return pw, browser, context, page


def _fake_playwright_for_login():
    """pw для login_context: launch_persistent_context → context (с .pages)."""
    pw = MagicMock()
    context = AsyncMock()
    context.pages = []  # заставит создать new_page
    page = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.storage_state = AsyncMock()
    pw.chromium.launch_persistent_context = AsyncMock(return_value=context)
    return pw, context, page


@pytest.fixture(autouse=True)
def profiles_root(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_PROFILES_DIR", str(tmp_path / "browser_profiles"))
    return tmp_path / "browser_profiles"


async def test_open_page_loads_storage_state_when_file_exists(profiles_root):
    """publish: если <root>/<channel>_state.json есть — он передан в new_context."""
    state_file = bb.storage_state_path("yandex")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"cookies": [], "origins": []}')

    pw, browser, context, page = _fake_playwright_for_publish()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.open_page("yandex", headless=True) as p:
            assert p is page

    browser.new_context.assert_awaited_once()
    assert browser.new_context.call_args.kwargs["storage_state"] == str(state_file)
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()


async def test_open_page_no_storage_state_when_file_absent(profiles_root):
    """publish: если файла сессии нет — storage_state=None (контекст пустой)."""
    pw, browser, context, page = _fake_playwright_for_publish()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.open_page("yandex", headless=True):
            pass

    assert browser.new_context.call_args.kwargs["storage_state"] is None


# ── UI-режим: явный per-profile storage_state (изоляция) ──────────────────────


async def test_open_page_uses_explicit_dict_and_ignores_file(profiles_root):
    """UI: storage_state передан явно (dict) — он в new_context, ФАЙЛ не читаем."""
    # Даже если общий файл существует — он не должен подхватиться.
    state_file = bb.storage_state_path("vk_wall")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"cookies": [{"name": "shared"}], "origins": []}')

    explicit = {"cookies": [{"name": "profileA"}], "origins": []}
    pw, browser, context, page = _fake_playwright_for_publish()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.open_page("vk_wall", headless=True, storage_state=explicit):
            pass

    assert browser.new_context.call_args.kwargs["storage_state"] == explicit


async def test_open_page_explicit_none_does_not_fall_back_to_file(profiles_root):
    """UI-изоляция: storage_state=None + существующий файл → контекст ПУСТОЙ."""
    state_file = bb.storage_state_path("vk_wall")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"cookies": [{"name": "shared"}], "origins": []}')

    pw, browser, context, page = _fake_playwright_for_publish()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.open_page("vk_wall", headless=True, storage_state=None):
            pass

    assert browser.new_context.call_args.kwargs["storage_state"] is None


async def test_open_page_coerces_json_string(profiles_root):
    """UI: storage_state как JSON-строка (из credentials) → dict в new_context."""
    pw, browser, context, page = _fake_playwright_for_publish()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.open_page(
            "vk_wall", headless=True, storage_state='{"cookies": [], "origins": []}'
        ):
            pass

    assert browser.new_context.call_args.kwargs["storage_state"] == {"cookies": [], "origins": []}


async def test_login_context_exports_storage_state_on_save(profiles_root):
    """login: save_state() экспортирует storageState в storage_state_path(channel)."""
    pw, context, page = _fake_playwright_for_login()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.login_context("yandex", headless=False) as (p, save_state):
            assert p is page
            returned = await save_state()

    # persistent-контекст поднят с user-data-dir = profile_dir
    pw.chromium.launch_persistent_context.assert_awaited_once()
    assert pw.chromium.launch_persistent_context.call_args.args[0] == str(bb.profile_dir("yandex"))

    # storage_state экспортирован в правильный файл
    context.storage_state.assert_awaited_once()
    assert context.storage_state.call_args.kwargs["path"] == str(bb.storage_state_path("yandex"))
    assert returned == bb.storage_state_path("yandex")
    context.close.assert_awaited_once()


# ── is_logged_in: DOM-факты, не только URL ────────────────────────────────────


class _MarkerPage:
    """Страница с управляемым присутствием DOM-маркеров."""

    def __init__(self, url: str, present: set[str]) -> None:
        self.url = url
        self._present = present

    async def query_selector(self, sel):
        return object() if sel in self._present else None

    async def wait_for_selector(self, sel, **k):
        if sel in self._present:
            return object()
        raise RuntimeError("not found")


async def test_is_logged_in_false_when_logged_out_selector_present():
    """URL «чистый», но на странице форма входа → НЕ залогинен (факт важнее URL)."""
    page = _MarkerPage("https://vk.com/feed", present={"text=Войти"})
    result = await bb.is_logged_in(
        page,
        reject_url_fragments=("vk.com/login",),
        reject_selectors=("text=Войти",),
    )
    assert result is False


async def test_is_logged_in_true_when_no_logged_out_markers():
    page = _MarkerPage("https://vk.com/feed", present=set())
    result = await bb.is_logged_in(
        page,
        reject_url_fragments=("vk.com/login",),
        reject_selectors=("text=Войти", "text=/Добро пожаловать/i"),
    )
    assert result is True


async def test_is_logged_in_url_reject_still_wins():
    page = _MarkerPage("https://vk.com/login", present=set())
    result = await bb.is_logged_in(page, reject_url_fragments=("vk.com/login",))
    assert result is False
