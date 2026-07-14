"""Контракт адаптера канала. Эпик 0.

Каждый из 8 каналов реализует ОДИН интерфейс. Контракт async с самого начала
(Telethon и Playwright асинхронны). Граница API↔браузер проходит на уровне
подпакетов adapters/api и adapters/browser — не смешивать.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from crosspost.content.canonical import CanonicalContent


class ResultStatus(StrEnum):
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # идемпотентность: уже опубликовано
    SUBMITTED = "submitted"  # fire-and-forget: ушло на модерацию (Яндекс Бизнес)
    NEEDS_RELOGIN = "needs_relogin"  # сессия протухла — нужен ручной логин


@dataclass(frozen=True)
class ChannelResult:
    """Итог публикации в один канал.

    external_id — КВИТАНЦИЯ об успехе (для верификации/правки/удаления),
    НЕ ключ дедупа. Дедуп — по внутреннему publication_id (см. orchestrator.task).
    """

    channel: str
    status: ResultStatus
    external_id: str | None = None
    error: str | None = None


@runtime_checkable
class ChannelAdapter(Protocol):
    """Все каналы реализуют это. Никаких спецслучаев в оркестраторе."""

    #: имя канала, напр. "telegram"
    channel: str

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult: ...
