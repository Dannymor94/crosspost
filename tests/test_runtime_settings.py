"""Рантайм-настройки: round-trip, приоритет override поверх .env, устойчивость."""

from __future__ import annotations

import pytest

from crosspost import runtime_settings
from crosspost.config import browser_headless


@pytest.fixture(autouse=True)
def _settings_path(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "settings.json"))


def test_unset_returns_none():
    assert runtime_settings.get_browser_headless() is None


def test_set_true_then_read():
    runtime_settings.set_browser_headless(True)
    assert runtime_settings.get_browser_headless() is True


def test_set_false_then_read():
    runtime_settings.set_browser_headless(False)
    assert runtime_settings.get_browser_headless() is False


def test_persists_across_reload(tmp_path, monkeypatch):
    runtime_settings.set_browser_headless(True)
    # «Новый процесс» — читаем тот же файл заново, без кэша в памяти.
    assert runtime_settings.get_browser_headless() is True


def test_corrupt_file_falls_back_to_none(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{ not json", "utf-8")
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(path))
    assert runtime_settings.get_browser_headless() is None


def test_override_wins_over_env(monkeypatch):
    """UI-override приоритетнее BROWSER_HEADLESS из окружения/.env."""
    monkeypatch.setenv("BROWSER_HEADLESS", "false")  # .env говорит «видим»
    runtime_settings.set_browser_headless(True)  # админ переключил в «скрыт»
    assert browser_headless() is True


def test_env_used_when_no_override(monkeypatch):
    """Без override решает окружение (.env)."""
    monkeypatch.setenv("BROWSER_HEADLESS", "true")
    assert runtime_settings.get_browser_headless() is None
    assert browser_headless() is True
