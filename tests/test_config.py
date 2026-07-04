"""Тесты parse_bool из config.py."""
from __future__ import annotations

import pytest

from crosspost.config import parse_bool


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "No", "off", "Off", ""])
def test_parse_bool_falsy(value: str) -> None:
    assert parse_bool(value) is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "Yes", "on", "anything"])
def test_parse_bool_truthy(value: str) -> None:
    assert parse_bool(value) is True


def test_parse_bool_none_returns_default() -> None:
    assert parse_bool(None, default=True) is True
    assert parse_bool(None, default=False) is False
