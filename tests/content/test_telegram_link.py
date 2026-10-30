"""Tests for parse_telegram_link — RED until implementation."""

import pytest

from crosspost.content.telegram_text import parse_telegram_link


@pytest.mark.parametrize(
    "url, expected_value, expected_topic",
    [
        # numeric channel with sub-thread → take thread root (42)
        ("https://t.me/c/1234567890/42/57", "-1001234567890", 42),
        # numeric channel without sub-thread
        ("https://t.me/c/1234567890/42", "-1001234567890", 42),
        # username channel with sub-thread → take thread root (42)
        ("https://t.me/somegroup/42/57", "@somegroup", 42),
        # username channel without sub-thread
        ("https://t.me/somegroup/42", "@somegroup", 42),
        # General topic (id=1) → None
        ("https://t.me/c/1234567890/1", "-1001234567890", None),
        ("https://t.me/somegroup/1", "@somegroup", None),
        # t.me without https
        ("t.me/c/9876543210/5", "-1009876543210", 5),
    ],
)
def test_parse_telegram_link_ok(url, expected_value, expected_topic):
    value, topic = parse_telegram_link(url)
    assert value == expected_value
    assert topic == expected_topic


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/somegroup",  # no message id at all
        "https://t.me/c/1234567890",  # numeric, no message id
        "https://vk.com/group/42",  # wrong domain
        "not a url",
        "",
    ],
)
def test_parse_telegram_link_invalid(url):
    with pytest.raises(ValueError):
        parse_telegram_link(url)
