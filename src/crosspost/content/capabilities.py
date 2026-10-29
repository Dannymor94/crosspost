"""Capability-матрица: какой канал какие type принимает. Эпик 1.

Несущий артефакт: определяет доступные чекбоксы в UI и валидирует отправку
ДО очереди. Без неё UI предложит бессмыслицу (reel -> Telegraph).
TODO: уточнить по мере реализации адаптеров.
"""

from __future__ import annotations

from crosspost.content.canonical import ContentType as T

# Channels that can receive video posts (mp4/mov/etc.).
# Only channels with an implemented video-sending adapter are listed.
VIDEO_CHANNELS: frozenset[str] = frozenset({"telegram", "instagram"})

# Channels that REQUIRE video — cannot post without a video attachment.
VIDEO_REQUIRED_CHANNELS: frozenset[str] = frozenset({"instagram"})

CAPABILITIES: dict[str, set[T]] = {
    # API-тир
    "telegram": {T.POST, T.ARTICLE, T.REEL, T.STORY},
    "vk": {T.POST, T.ARTICLE, T.REEL, T.STORY},
    "youtube": {T.REEL},
    "telegraph": {T.ARTICLE},
    # Instagram (API-тир, Content Publishing API).
    # T.POST: video post → adapter publishes as Reel internally; VIDEO_REQUIRED_CHANNELS
    # ensures no-video posts are rejected at the eligibility gate, not here.
    "instagram": {T.POST, T.REEL},
    # браузерный тир
    "whatsapp": {T.POST, T.STORY},
    "dzen": {T.ARTICLE, T.POST},
    "yandex": {T.POST},
    "vk_wall": {T.POST},
    "vk_channel": {T.POST},
}


def video_ineligible_reason(
    channel: str, max_video_bytes: int | None, *, has_video: bool, video_bytes: int = 0
) -> str:
    """Причина видео-неприемлемости канала, или "" если по видео всё ок.

    ЕДИНЫЙ источник правила: его зовут и GET /publish/targets (строит reason),
    и _reject_ineligible (серверный гейт перед отправкой), и импорт контент-плана.
    Тексты причин одинаковы во всех трёх местах — не дублировать правило.
    """
    if has_video and channel not in VIDEO_CHANNELS:
        return "Этот канал не принимает видео"
    if has_video and video_bytes > 0 and max_video_bytes and video_bytes > max_video_bytes:
        return f"Видео больше лимита канала ({max_video_bytes // (1024 * 1024)} МБ)"
    if not has_video and channel in VIDEO_REQUIRED_CHANNELS:
        return "Прикрепите видео для публикации"
    return ""


def supports(channel: str, content_type: T) -> bool:
    return content_type in CAPABILITIES.get(channel, set())


def supports_video(channel: str) -> bool:
    """True if channel has an implemented video-sending adapter."""
    return channel in VIDEO_CHANNELS


def channels_for(content_type: T) -> set[str]:
    return {ch for ch, types in CAPABILITIES.items() if content_type in types}
