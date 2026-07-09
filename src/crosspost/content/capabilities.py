"""Capability-матрица: какой канал какие type принимает. Эпик 1.

Несущий артефакт: определяет доступные чекбоксы в UI и валидирует отправку
ДО очереди. Без неё UI предложит бессмыслицу (reel -> Telegraph).
TODO: уточнить по мере реализации адаптеров.
"""
from __future__ import annotations

from crosspost.content.canonical import ContentType as T

CAPABILITIES: dict[str, set[T]] = {
    # API-тир
    "telegram":  {T.POST, T.ARTICLE, T.REEL, T.STORY},
    "vk":        {T.POST, T.ARTICLE, T.REEL, T.STORY},
    "youtube":   {T.REEL},
    "telegraph": {T.ARTICLE},
    # браузерный тир
    "whatsapp":  {T.POST, T.STORY},
    "instagram": {T.POST, T.REEL, T.STORY},
    "dzen":      {T.ARTICLE, T.POST},
    "yandex":    {T.POST},
    "vk_wall":   {T.POST},
    "vk_channel": {T.POST},
}


def supports(channel: str, content_type: T) -> bool:
    return content_type in CAPABILITIES.get(channel, set())


def channels_for(content_type: T) -> set[str]:
    return {ch for ch, types in CAPABILITIES.items() if content_type in types}
