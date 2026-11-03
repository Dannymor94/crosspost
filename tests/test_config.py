"""Тесты parse_bool и load_config из config.py."""

from __future__ import annotations

import pytest

from crosspost.config import load_config, parse_bool


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "No", "off", "Off", ""])
def test_parse_bool_falsy(value: str) -> None:
    assert parse_bool(value) is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "Yes", "on", "anything"])
def test_parse_bool_truthy(value: str) -> None:
    assert parse_bool(value) is True


def test_parse_bool_none_returns_default() -> None:
    assert parse_bool(None, default=True) is True
    assert parse_bool(None, default=False) is False


def test_env_only_key_visible_in_load_config(monkeypatch, tmp_path) -> None:
    """Key set only in env (not in .env) must appear in load_config() result."""
    monkeypatch.setenv("CROSSPOST_TEST_SENTINEL", "hello")
    cfg = load_config(tmp_path / "nonexistent.env")
    assert cfg.get("CROSSPOST_TEST_SENTINEL") == "hello"
