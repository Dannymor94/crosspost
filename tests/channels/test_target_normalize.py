"""Тесты нормализации цели постинга. Итерация 2а (баг задвоения адреса).

Из любого формата ввода вытягиваем чистое значение; мусор → человеческая ошибка.
"""

from __future__ import annotations

import pytest

from crosspost.channels.validators import (
    TargetError,
    normalize_vk_channel_id,
    normalize_vk_screen_name,
    normalize_yandex_org,
)


@pytest.mark.parametrize(
    "raw",
    [
        "https://vk.com/yogapiecom",
        "http://vk.com/yogapiecom",
        "vk.com/yogapiecom",
        "www.vk.com/yogapiecom",
        "m.vk.com/yogapiecom",
        "@yogapiecom",
        "yogapiecom",
        "  vk.com/yogapiecom/  ",
        "https://vk.com/yogapiecom?w=wall-1_2",
    ],
)
def test_vk_screen_name_extracted(raw):
    assert normalize_vk_screen_name(raw) == "yogapiecom"


def test_vk_screen_name_rejects_garbage():
    with pytest.raises(TargetError, match="группы VK"):
        normalize_vk_screen_name("https://example.com/foo bar")


def test_vk_channel_id():
    assert normalize_vk_channel_id("-240033402") == "-240033402"
    assert normalize_vk_channel_id("id -240033402 канал") == "-240033402"
    with pytest.raises(TargetError):
        normalize_vk_channel_id("нет числа")


def test_yandex_org():
    assert normalize_yandex_org("https://yandex.ru/sprav/123456789/edit") == "123456789"
    assert normalize_yandex_org("123456789") == "123456789"
    with pytest.raises(TargetError):
        normalize_yandex_org("нет id")
