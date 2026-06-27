"""Валидация CanonicalContent по типу. Эпик 1. TODO: дополнить правила."""
from __future__ import annotations

from crosspost.content.canonical import CanonicalContent, ContentType


class ContentValidationError(ValueError):
    pass


def validate(content: CanonicalContent) -> None:
    t = content.type
    if t in (ContentType.REEL, ContentType.STORY) and not content.media_paths:
        raise ContentValidationError(f"{t}: требуется видео")
    if t == ContentType.POST and not content.media_paths:
        raise ContentValidationError("post: требуется изображение")
    if t == ContentType.ARTICLE and not content.title:
        raise ContentValidationError("article: требуется заголовок")
