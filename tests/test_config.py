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


def test_tg_api_id_alias(monkeypatch, tmp_path, caplog) -> None:
    """TG_API_ID/TG_API_HASH are deprecated aliases; warning goes to logger."""
    import logging

    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "abc")
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    with caplog.at_level(logging.WARNING, logger="crosspost.config"):
        cfg = load_config(tmp_path / "nonexistent.env")
    assert cfg["TELEGRAM_API_ID"] == "12345"
    assert cfg["TELEGRAM_API_HASH"] == "abc"
    assert any("TG_API_ID" in r.message for r in caplog.records)


def test_tg_alias_used_when_canonical_empty(monkeypatch, tmp_path) -> None:
    """Empty TELEGRAM_API_ID is treated as not set; legacy TG_API_ID alias applies."""
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_ID", "")
    cfg = load_config(tmp_path / "nonexistent.env")
    assert cfg["TELEGRAM_API_ID"] == "12345"


def test_canonical_name_takes_precedence(monkeypatch, tmp_path) -> None:
    """Non-empty TELEGRAM_API_ID wins over TG_API_ID."""
    monkeypatch.setenv("TG_API_ID", "legacy")
    monkeypatch.setenv("TELEGRAM_API_ID", "canonical")
    cfg = load_config(tmp_path / "nonexistent.env")
    assert cfg["TELEGRAM_API_ID"] == "canonical"
