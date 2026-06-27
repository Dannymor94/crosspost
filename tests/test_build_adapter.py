"""Тесты фабрики build_adapter (фаза 0, шаг 4).

Mock-first: конфиг и конструкторы клиентов мокаются — НИ сети, НИ ключей.
Проверяем: правильный тип адаптера на telegram/vk и осмысленную ошибку на
неизвестном/браузерном канале. Ленивые импорты telethon/vkbottle подменяются
через sys.modules — пакеты ставить не нужно.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from crosspost import __main__ as cli
from crosspost.adapters.api.telegram import TelegramAdapter
from crosspost.adapters.api.vk import VKAdapter

FAKE_CFG = {
    "TG_API_ID": "123",
    "TG_API_HASH": "deadbeefhash",
    "TG_TARGET_CHANNEL": "@my_channel",
    "TG_SESSION_PATH": "runtime/sessions/telegram.session",
    "VK_ACCESS_TOKEN": "vk-token",
    "VK_GROUP_ID": "100",
}


@pytest.fixture(autouse=True)
def patched_config(monkeypatch):
    """Подменяем загрузку конфига — тесты не читают runtime/.env."""
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: dict(FAKE_CFG))


@pytest.fixture
def fake_telethon(monkeypatch):
    mod = types.ModuleType("telethon")
    mod.TelegramClient = MagicMock(return_value=MagicMock(name="TelegramClient()"))
    monkeypatch.setitem(sys.modules, "telethon", mod)
    return mod


@pytest.fixture
def fake_vkbottle(monkeypatch):
    mod = types.ModuleType("vkbottle")
    mod.API = MagicMock(return_value=MagicMock(name="API()"))
    monkeypatch.setitem(sys.modules, "vkbottle", mod)
    return mod


def test_builds_telegram_adapter(store, fake_telethon):
    adapter = cli.build_adapter("telegram", store)

    assert isinstance(adapter, TelegramAdapter)
    assert adapter.channel == "telegram"
    fake_telethon.TelegramClient.assert_called_once()  # клиент собран без сети


def test_builds_vk_adapter(store, fake_vkbottle):
    adapter = cli.build_adapter("vk", store)

    assert isinstance(adapter, VKAdapter)
    assert adapter.channel == "vk"
    fake_vkbottle.API.assert_called_once()  # VK-сессия собрана без сети


def test_unknown_channel_raises_clear_error(store):
    with pytest.raises(ValueError, match="неизвестн"):
        cli.build_adapter("myspace", store)


def test_browser_channel_rejected(store):
    """Браузерный канал в API-фабрику не пускаем (граница API↔браузер)."""
    with pytest.raises(ValueError, match="браузер"):
        cli.build_adapter("instagram", store)
