"""Тесты validate(): POST допускает текст ИЛИ фото; пустой пост — ошибка."""

from __future__ import annotations

from pathlib import Path

import pytest

from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.content.validation import ContentValidationError, validate


def test_post_text_only_is_valid():
    validate(CanonicalContent(type=ContentType.POST, text="только текст", media_paths=[]))


def test_post_media_only_is_valid():
    validate(CanonicalContent(type=ContentType.POST, text="", media_paths=[Path("a.jpg")]))


def test_post_empty_is_invalid():
    with pytest.raises(ContentValidationError, match="текст или изображение"):
        validate(CanonicalContent(type=ContentType.POST, text="   ", media_paths=[]))


def test_reel_still_requires_media():
    with pytest.raises(ContentValidationError, match="видео"):
        validate(CanonicalContent(type=ContentType.REEL, text="подпись", media_paths=[]))
