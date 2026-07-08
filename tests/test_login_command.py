"""Тесты CLI-команды `crosspost login` — без реального браузера.

login_context патчится в base_browser; ввод пользователя — через _input.
Источник сессии — storageState-файл: login экспортирует его через save_state().
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from crosspost import __main__ as cli
from crosspost.adapters.browser.base_browser import profile_dir, storage_state_path


@pytest.fixture(autouse=True)
def patched_config(monkeypatch, tmp_path):
    cfg = {
        "BROWSER_PROFILES_DIR": str(tmp_path / "browser_profiles"),
        "BROWSER_HEADLESS": "false",
    }
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: dict(cfg))
    return cfg


def _patch_login_context(page, save_state, *, url_after_login="https://yandex.ru/sprav/123"):
    """Подменить login_context: yield (page, save_state); page.url — «залогинен»."""
    page.url = url_after_login

    @asynccontextmanager
    async def _ctx(*args, **kwargs):
        yield page, save_state

    return patch("crosspost.adapters.browser.base_browser.login_context", _ctx)


async def test_login_yandex_opens_browser_and_saves_state():
    """login --channel yandex: goto Яндекса, ждёт Enter, экспортирует storageState."""
    page = AsyncMock()
    page.goto = AsyncMock()
    save_state = AsyncMock(return_value=Path("/x/yandex_state.json"))
    called = []

    def fake_input():
        called.append(True)

    with _patch_login_context(page, save_state):
        result = await cli._run_login("yandex", {}, _input=fake_input)

    assert result == 0
    page.goto.assert_awaited_once()
    assert "yandex.ru" in page.goto.call_args.args[0]
    assert called, "_input (ожидание Enter) не был вызван"
    save_state.assert_awaited_once()  # storageState экспортирован


async def test_login_vk_opens_browser_and_saves_state():
    """login --channel vk: goto vk.com, storageState экспортирован."""
    page = AsyncMock()
    page.goto = AsyncMock()
    save_state = AsyncMock(return_value=Path("/x/vk_state.json"))

    with _patch_login_context(page, save_state, url_after_login="https://vk.com/feed"):
        result = await cli._run_login("vk", {}, _input=lambda: None)

    assert result == 0
    assert "vk.com" in page.goto.call_args.args[0]
    save_state.assert_awaited_once()


async def test_login_reprompts_when_still_on_login_page():
    """Если после Enter URL всё ещё страница логина — не сохраняем, ждём ещё раз."""
    page = AsyncMock()
    page.goto = AsyncMock()
    save_state = AsyncMock(return_value=Path("/x/yandex_state.json"))

    # первый Enter → всё ещё passport; второй Enter → уже кабинет
    urls = iter([
        "https://passport.yandex.ru/auth",   # проверка после 1-го Enter → не залогинен
        "https://yandex.ru/sprav/123",        # проверка после 2-го Enter → залогинен
    ])
    inputs = []

    def fake_input():
        inputs.append(True)
        # переключаем URL страницы к следующей проверке
        page.url = next(urls)

    # начальный URL — страница логина, чтобы первая проверка провалилась
    page.url = "https://passport.yandex.ru/auth"

    @asynccontextmanager
    async def _ctx(*args, **kwargs):
        yield page, save_state

    with patch("crosspost.adapters.browser.base_browser.login_context", _ctx):
        result = await cli._run_login("yandex", {}, _input=fake_input)

    assert result == 0
    assert len(inputs) == 2, "ожидали повторный запрос Enter после незавершённого входа"
    save_state.assert_awaited_once()  # сохранили только после подтверждённого входа


async def test_login_unsupported_channel_returns_error():
    """login --channel instagram: не поддерживается → код 1, браузер не открывается."""
    with patch("crosspost.adapters.browser.base_browser.login_context") as mock_lc:
        result = await cli._run_login("instagram", {})

    assert result == 1
    mock_lc.assert_not_called()


def test_login_and_publish_resolve_same_state_file(monkeypatch, tmp_path):
    """storage_state_path(channel) — единый источник пути к сессии для login и publish."""
    custom_root = str(tmp_path / "browser_profiles")
    monkeypatch.setenv("BROWSER_PROFILES_DIR", custom_root)

    a = storage_state_path("yandex")
    b = storage_state_path("yandex")

    assert a == b
    assert a == Path(custom_root) / "yandex_state.json"

    # разные каналы → разные файлы в одном корне
    assert storage_state_path("yandex") != storage_state_path("vk")
    assert storage_state_path("yandex").parent == storage_state_path("vk").parent
    # profile_dir (кэш) живёт в том же корне
    assert profile_dir("yandex").parent == storage_state_path("yandex").parent


def test_login_main_parses_args(monkeypatch):
    """main(['login', '--channel', 'yandex']) доходит до _run_login без ошибок парсинга."""
    called_with = []

    async def fake_run_login(channel, cfg, **kwargs):
        called_with.append(channel)
        return 0

    monkeypatch.setattr(cli, "_run_login", fake_run_login)
    result = cli.main(["login", "--channel", "yandex"])

    assert result == 0
    assert called_with == ["yandex"]
