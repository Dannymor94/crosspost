"""TDD: platform grouping fields on channel declarations."""

from __future__ import annotations

import pytest

from crosspost.channels.validators import PLATFORM_ORDER, VALIDATORS


def test_every_declared_channel_has_platform():
    for ch, v in VALIDATORS.items():
        assert v.platform, f"{ch}: platform must be non-empty"
        assert v.platform_title, f"{ch}: platform_title must be non-empty"


def test_platform_order_covers_all_platforms():
    declared = {v.platform for v in VALIDATORS.values()}
    missing = declared - set(PLATFORM_ORDER)
    assert not missing, f"PLATFORM_ORDER is missing platforms: {missing}"


def test_platform_order_is_list_of_strings():
    assert isinstance(PLATFORM_ORDER, list)
    assert all(isinstance(p, str) for p in PLATFORM_ORDER)


@pytest.mark.parametrize(
    "channel, platform, platform_title, short_label",
    [
        ("telegram", "telegram", "Telegram", ""),
        ("telegraph", "telegraph", "Telegraph", "Статьи"),
        ("instagram", "instagram", "Instagram", "Reels"),
        ("yandex", "yandex", "Яндекс Бизнес", "Бизнес"),
        ("vk_wall", "vk", "ВКонтакте", "Стена"),
        ("vk_channel", "vk", "ВКонтакте", "Канал"),
    ],
)
def test_channel_platform_values(channel, platform, platform_title, short_label):
    v = VALIDATORS[channel]
    assert v.platform == platform
    assert v.platform_title == platform_title
    assert v.short_label == short_label


def test_platform_order_sequence():
    assert PLATFORM_ORDER == ["telegram", "vk", "instagram", "dzen", "yandex", "telegraph"]


def test_enabled_non_telegram_channels_have_short_label():
    """Every enabled non-telegram channel must have a non-empty short_label."""
    for ch, v in VALIDATORS.items():
        if not v.enabled or ch == "telegram":
            continue
        assert v.short_label, f"{ch}: enabled non-telegram channel must have non-empty short_label"
