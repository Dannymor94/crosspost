"""PublishService — оркестрация публикации по каналам. Итерация 2а.

Детерминированная, поканальная, изолированная по profile_id (через repo/factory).
Адаптеры — «тупые и изолированные»: падение одного не рушит другие.

Инварианты (CLAUDE.md):
  - идемпотентность по внутреннему publication_id: канал уже done → пропустить;
  - частичный успех — норма: статусы поканальные и независимые;
  - ретрай — только упавшего канала, без повторной отправки в успешные;
  - контент только в поддерживаемые типы (capability-матрица) — отсекаем ДО отправки.

Адаптеры получаем через инъекцию (adapter_factory) — сервис не знает, как они
строятся; в тестах подменяются моками площадок.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from crosspost.adapters.base import ChannelAdapter, ResultStatus
from crosspost.content.canonical import CanonicalContent
from crosspost.content.capabilities import supports
from crosspost.content.validation import validate
from crosspost.db.models import PublicationStatus
from crosspost.db.publication_repo import PublicationRepository

logger = logging.getLogger(__name__)

# async (channel) -> адаптер, либо None если нет активного подключения/учётки.
AdapterFactory = Callable[[str], Awaitable[ChannelAdapter | None]]

# ResultStatus адаптера → PublicationStatus для БД/UI.
_RESULT_MAP: dict[ResultStatus, PublicationStatus] = {
    ResultStatus.DONE: PublicationStatus.DONE,
    ResultStatus.SUBMITTED: PublicationStatus.SUBMITTED,
    ResultStatus.SKIPPED: PublicationStatus.DONE,  # уже опубликовано — считаем done
    ResultStatus.FAILED: PublicationStatus.FAILED,
    ResultStatus.NEEDS_RELOGIN: PublicationStatus.NEEDS_RELOGIN,
}


@dataclass
class ChannelOutcome:
    """Итог по одному каналу (для UI)."""

    channel: str
    status: str  # значение PublicationStatus
    external_id: str | None = None
    error: str | None = None


class PublishService:
    def __init__(self, repo: PublicationRepository, adapter_factory: AdapterFactory) -> None:
        self._repo = repo
        self._factory = adapter_factory

    async def publish(
        self,
        content: CanonicalContent,
        channels: list[str],
        *,
        publication_id: str,
    ) -> list[ChannelOutcome]:
        """Опубликовать во все каналы. Возвращает поканальные итоги (частичный успех — норма)."""
        validate(content)  # общая валидация контента (не поканальная)
        outcomes: list[ChannelOutcome] = []
        for channel in channels:
            outcomes.append(await self._publish_one(content, channel, publication_id))
        return outcomes

    async def retry_channel(
        self,
        content: CanonicalContent,
        channel: str,
        *,
        publication_id: str,
    ) -> ChannelOutcome:
        """Повторить ОДИН канал. Уже done/submitted → не трогаем (без дублей)."""
        if await self._repo.is_done(publication_id, channel):
            return await self._existing_outcome(publication_id, channel)
        return await self._publish_one(content, channel, publication_id)

    # ── внутреннее ────────────────────────────────────────────────────────────

    async def _publish_one(
        self, content: CanonicalContent, channel: str, publication_id: str
    ) -> ChannelOutcome:
        # (1) capability: не диспатчим неподдерживаемый тип
        if not supports(channel, content.type):
            msg = f"канал '{channel}' не поддерживает тип {content.type.value}"
            await self._repo.set_status(
                publication_id, channel, PublicationStatus.FAILED, error=msg
            )
            return ChannelOutcome(channel, PublicationStatus.FAILED.value, error=msg)

        # (2) идемпотентность: уже done → пропустить (без повторной отправки)
        if await self._repo.is_done(publication_id, channel):
            return await self._existing_outcome(publication_id, channel)

        # (3) помечаем attempting ДО диспатча (окно «ушло→упал» видно в UI)
        await self._repo.set_status(publication_id, channel, PublicationStatus.ATTEMPTING)

        # (4) строим адаптер под профиль
        try:
            adapter = await self._factory(channel)
        except Exception as exc:  # noqa: BLE001 — падение сборки не рушит другие каналы
            logger.warning("adapter build failed %s: %s", channel, exc)
            await self._repo.set_status(
                publication_id, channel, PublicationStatus.FAILED, error=f"сборка канала: {exc}"
            )
            return ChannelOutcome(channel, PublicationStatus.FAILED.value, error=str(exc))

        if adapter is None:
            msg = "нет активного подключения — переподключите канал в настройках"
            await self._repo.set_status(
                publication_id, channel, PublicationStatus.NEEDS_RELOGIN, error=msg
            )
            return ChannelOutcome(channel, PublicationStatus.NEEDS_RELOGIN.value, error=msg)

        # (5) публикуем; падение адаптера → FAILED, но не роняет соседей
        try:
            result = await adapter.publish(content, publication_id=publication_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("publish failed %s: %s", channel, exc)
            await self._repo.set_status(
                publication_id, channel, PublicationStatus.FAILED, error=str(exc)
            )
            return ChannelOutcome(channel, PublicationStatus.FAILED.value, error=str(exc))

        # (6) маппим квитанцию → статус
        status = _RESULT_MAP.get(result.status, PublicationStatus.FAILED)
        await self._repo.set_status(
            publication_id,
            channel,
            status,
            external_id=result.external_id,
            error=result.error,
        )
        return ChannelOutcome(
            channel, status.value, external_id=result.external_id, error=result.error
        )

    async def _existing_outcome(self, publication_id: str, channel: str) -> ChannelOutcome:
        row = await self._repo.get_status(publication_id, channel)
        if row is None:
            return ChannelOutcome(channel, PublicationStatus.DONE.value)
        return ChannelOutcome(
            channel, str(row.status), external_id=row.external_id, error=row.error
        )
