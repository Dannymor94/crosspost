"""Тесты фабрики build_adapter (фаза 0, шаги 4–5).

Mock-first: конфиг и SDK (telethon/vkbottle) мокаются через sys.modules —
НИ сети, НИ ключей, пакеты ставить не нужно.

Шаг 5 (smoke-проводка): для telegram фабрика поднимает реальный TelegramClient
один раз — await start() (разовый логин) + сохранение StringSession в
TG_SESSION_PATH, чтобы повторные запуски не просили код. Поэтому build_adapter
теперь корутина.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from crosspost import __main__ as cli
from crosspost.adapters.api.telegram import TelegramAdapter
from crosspost.adapters.api.vk import VKAdapter


@pytest.fixture
def fake_cfg(tmp_path) -> dict[str, str]:
    # TG_SESSION_PATH — во временный каталог: тест пишет сюда сохранённую сессию
    return {
        "TG_API_ID": "123",
        "TG_API_HASH": "deadbeefhash",
        "TG_TARGET_CHANNEL": "@my_channel",
        "TG_SESSION_PATH": str(tmp_path / "sessions" / "tg.session"),
        "VK_ACCESS_TOKEN": "vk-token",
        "VK_GROUP_ID": "100",
    }


@pytest.fixture(autouse=True)
def patched_config(monkeypatch, fake_cfg):
    """Подменяем загрузку конфига — тесты не читают runtime/.env."""
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: dict(fake_cfg))


@pytest.fixture
def fake_telethon(monkeypatch):
    """telethon: TelegramClient с async start() и session.save() -> строка."""
    mod = types.ModuleType("telethon")
    client = MagicMock(name="TelegramClient()")
    client.start = AsyncMock()
    client.session.save.return_value = "SAVED_STRING_SESSION"
    mod.TelegramClient = MagicMock(name="TelegramClient", return_value=client)
    monkeypatch.setitem(sys.modules, "telethon", mod)

    sessions_mod = types.ModuleType("telethon.sessions")
    sessions_mod.StringSession = MagicMock(name="StringSession")
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions_mod)
    return mod


@pytest.fixture
def fake_vkbottle(monkeypatch):
    mod = types.ModuleType("vkbottle")
    mod.API = MagicMock(name="API", return_value=MagicMock(name="API()"))
    monkeypatch.setitem(sys.modules, "vkbottle", mod)
    return mod


async def test_builds_telegram_adapter_starts_and_persists_session(store, fake_cfg, fake_telethon):
    adapter = await cli.build_adapter("telegram", store)

    assert isinstance(adapter, TelegramAdapter)
    assert adapter.channel == "telegram"

    # клиент собран и стартован ровно один раз (разовый логин по коду)
    fake_telethon.TelegramClient.assert_called_once()
    client = fake_telethon.TelegramClient.return_value
    client.start.assert_awaited_once()

    # StringSession сохранён в TG_SESSION_PATH — повторный запуск кода не попросит
    assert Path(fake_cfg["TG_SESSION_PATH"]).read_text() == "SAVED_STRING_SESSION"


async def test_builds_vk_adapter(store, fake_vkbottle):
    adapter = await cli.build_adapter("vk", store)

    assert isinstance(adapter, VKAdapter)
    assert adapter.channel == "vk"
    fake_vkbottle.API.assert_called_once()  # VK-сессия собрана без сети


async def test_unknown_channel_raises_clear_error(store):
    with pytest.raises(ValueError, match="неизвестн"):
        await cli.build_adapter("myspace", store)


async def test_browser_channel_rejected(store):
    """Браузерный канал в API-фабрику не пускаем (граница API↔браузер)."""
    with pytest.raises(ValueError, match="браузер"):
        await cli.build_adapter("instagram", store)
