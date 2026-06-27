"""Каноническая модель контента. Эпик 1.

Один объект с полем type. Разные type — разные модели данных, не сливать.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ContentType(str, Enum):
    POST = "post"        # текст + картинка
    ARTICLE = "article"  # заголовок + тело + обложка
    REEL = "reel"        # постоянное вертикальное видео
    STORY = "story"      # эфемерное видео (24ч)


@dataclass
class CanonicalContent:
    type: ContentType
    text: str = ""
    title: str | None = None              # для article
    media_paths: list[Path] = field(default_factory=list)  # локальные temp-файлы
