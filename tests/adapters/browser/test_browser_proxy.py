"""Тесты T-204: proxy_env_var для браузерных каналов vk_wall, vk_channel, yandex.

Проверяем, что adapter_factory._build_browser читает прокси из env-переменной
и передаёт адаптеру.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from crosspost.orchestrator.adapter_factory import _build_browser


@pytest.fixture
def store():
    return MagicMock()


@pytest.fixture(autouse=True)
def mock_headless():
    with patch("crosspost.orchestrator.adapter_factory.browser_headless", return_value=False):
        yield


def test_vk_wall_adapter_receives_proxy_from_env(monkeypatch, store):
    monkeypatch.setenv("VK_PROXY_URL", "http://ru-proxy:3128")
    adapter = _build_browser("vk_wall", "{}", store, "mygroup")
    assert adapter._proxy_url == "http://ru-proxy:3128"


def test_vk_channel_adapter_receives_proxy_from_env(monkeypatch, store):
    monkeypatch.setenv("VK_PROXY_URL", "http://ru-proxy:3128")
    adapter = _build_browser("vk_channel", "{}", store, "-123456789")
    assert adapter._proxy_url == "http://ru-proxy:3128"


def test_yandex_adapter_receives_proxy_from_env(monkeypatch, store):
    monkeypatch.setenv("YANDEX_PROXY_URL", "http://ru-proxy:8080")
    adapter = _build_browser("yandex", "{}", store, "org123")
    assert adapter._proxy_url == "http://ru-proxy:8080"


def test_adapter_proxy_url_empty_when_env_not_set(monkeypatch, store):
    monkeypatch.delenv("VK_PROXY_URL", raising=False)
    adapter = _build_browser("vk_wall", "{}", store, "mygroup")
    assert adapter._proxy_url == ""


def test_adapter_proxy_url_with_credentials(monkeypatch, store):
    """Прокси с логином:паролем хранится как есть в адаптере (парсинг — в base_browser)."""
    monkeypatch.setenv("VK_PROXY_URL", "http://user:secret@ru-proxy:3128")
    adapter = _build_browser("vk_wall", "{}", store, "mygroup")
    assert adapter._proxy_url == "http://user:secret@ru-proxy:3128"
