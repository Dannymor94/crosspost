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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from crosspost.adapters.base import ChannelAdapter, ResultStatus
from crosspost.content.article_link import DEFAULT_LINK_PHRASE, LINK_TEMPLATE, NO_ARTICLE_REASON
from crosspost.content.canonical import CanonicalContent
from crosspost.content.capabilities import supports
from crosspost.content.validation import validate
from crosspost.db.models import PublicationStatus
from crosspost.db.publication_repo import PublicationRepository
from crosspost.orchestrator.article_flow import (
    _ARTICLE_CHANNEL,
    StepResult,
    StepState,
    run_binding,
)

logger = logging.getLogger(__name__)

# Статусы, которые считаем успехом канала (для single-shot step связки статьи).
_SUCCESS_STATUSES = {PublicationStatus.DONE.value, PublicationStatus.SUBMITTED.value}


@dataclass(frozen=True)
class Selection:
    """Единица публикации: площадка + id цели (мультицели).

    target_id — стабильный id ChannelTarget (строкой), пусто "" — таргетлесс
    единица (Telegraph) и legacy-однокатегорийная публикация. Он же — измерение
    идемпотентности в publications (см. Блок 2). Строка-канал в старом коде
    эквивалентна Selection(channel, "") — см. _as_selection.
    """

    channel: str
    target_id: str = ""


def _as_selection(item: str | Selection) -> Selection:
    """Нормализовать элемент выбора: строка-канал → таргетлесс Selection."""
    return item if isinstance(item, Selection) else Selection(item, "")


# async (channel, target_id) -> адаптер, либо None если нет подключения/учётки/цели.
AdapterFactory = Callable[[str, str], Awaitable[ChannelAdapter | None]]

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
    """Итог по одной единице публикации (канал + цель) для UI."""

    channel: str
    status: str  # значение PublicationStatus
    external_id: str | None = None
    error: str | None = None
    screenshot_path: str | None = None  # диагностика браузерного сбоя (Блок 4)
    # Временная ссылка «открыть канал» из ChannelResult. НЕ пишется в БД —
    # живёт только в этом ответе (сразу после отправки), поллинг из БД её не вернёт.
    url: str | None = None
    # id цели (мультицели). "" — таргетлесс (Telegraph/legacy). Имя цели для показа
    # проставляет слой роутов (у него ChannelTargetRepository), см. Блок 5.
    target: str = ""


class PublishService:
    def __init__(self, repo: PublicationRepository, adapter_factory: AdapterFactory) -> None:
        self._repo = repo
        self._factory = adapter_factory

    async def publish(
        self,
        content: CanonicalContent,
        channels: Sequence[str | Selection],
        *,
        publication_id: str,
        article_text: str | None = None,
        article_title: str | None = None,
        link_phrase: str | None = None,
    ) -> list[ChannelOutcome]:
        """Опубликовать во все выбранные единицы (канал+цель). Частичный успех — норма.

        `channels` — площадки ИЛИ Selection'ы (канал+цель); строка = таргетлесс
        Selection(channel, ""). Одна площадка с несколькими целями = несколько
        Selection'ов с общим publication_id — каждая цель дедупится отдельно.

        article_text задан → сначала статья в Telegraph (таргетлесс), ссылка
        подставляется в текст поста, затем единицы. Секвенсирование — в общем
        run_binding; здесь step single-shot (потолка ретраев у немедленной нет).
        """
        validate(content)  # общая валидация контента (не поканальная)
        selections = [_as_selection(item) for item in channels]

        async def _step(sel: Selection, c: CanonicalContent) -> StepResult:
            outcome = await self._publish_one(c, sel, publication_id)
            state = StepState.DONE if outcome.status in _SUCCESS_STATUSES else StepState.FAILED
            return StepResult(state, outcome, outcome.external_id)

        async def _fail_closed(sel: Selection) -> ChannelOutcome:
            await self._repo.set_status(
                publication_id,
                sel.channel,
                PublicationStatus.FAILED,
                target=sel.target_id,
                error=NO_ARTICLE_REASON,
            )
            return ChannelOutcome(
                sel.channel,
                PublicationStatus.FAILED.value,
                error=NO_ARTICLE_REASON,
                target=sel.target_id,
            )

        flow = await run_binding(
            content=content,
            channels=selections,
            article_text=article_text or "",
            article_title=article_title,
            template=LINK_TEMPLATE,
            link_phrase=link_phrase or DEFAULT_LINK_PHRASE,
            step=_step,
            fail_closed=_fail_closed,
            article_item=Selection(_ARTICLE_CHANNEL, ""),
        )
        return flow.outcomes

    async def retry_channel(
        self,
        content: CanonicalContent,
        channel: str | Selection,
        *,
        publication_id: str,
    ) -> ChannelOutcome:
        """Повторить ОДНУ единицу (канал+цель). Уже done/submitted → не трогаем."""
        sel = _as_selection(channel)
        if await self._repo.is_done(publication_id, sel.channel, target=sel.target_id):
            return await self._existing_outcome(publication_id, sel)
        return await self._publish_one(content, sel, publication_id)

    # ── внутреннее ────────────────────────────────────────────────────────────

    async def _publish_one(
        self, content: CanonicalContent, sel: Selection, publication_id: str
    ) -> ChannelOutcome:
        channel, target_id = sel.channel, sel.target_id

        # (1) capability: не диспатчим неподдерживаемый тип
        if not supports(channel, content.type):
            msg = f"канал '{channel}' не поддерживает тип {content.type.value}"
            await self._repo.set_status(
                publication_id, channel, PublicationStatus.FAILED, target=target_id, error=msg
            )
            return ChannelOutcome(
                channel, PublicationStatus.FAILED.value, error=msg, target=target_id
            )

        # (2) идемпотентность: уже done → пропустить (без повторной отправки)
        if await self._repo.is_done(publication_id, channel, target=target_id):
            return await self._existing_outcome(publication_id, sel)

        # (3) помечаем attempting ДО диспатча (окно «ушло→упал» видно в UI)
        await self._repo.set_status(
            publication_id, channel, PublicationStatus.ATTEMPTING, target=target_id
        )

        # (4) строим адаптер под профиль (канал+цель)
        try:
            adapter = await self._factory(channel, target_id)
        except Exception as exc:  # noqa: BLE001 — падение сборки не рушит другие единицы
            logger.warning("adapter build failed %s/%s: %s", channel, target_id, exc)
            await self._repo.set_status(
                publication_id,
                channel,
                PublicationStatus.FAILED,
                target=target_id,
                error=f"сборка канала: {exc}",
            )
            return ChannelOutcome(
                channel, PublicationStatus.FAILED.value, error=str(exc), target=target_id
            )

        if adapter is None:
            msg = "нет активного подключения — переподключите канал в настройках"
            await self._repo.set_status(
                publication_id,
                channel,
                PublicationStatus.NEEDS_RELOGIN,
                target=target_id,
                error=msg,
            )
            return ChannelOutcome(
                channel, PublicationStatus.NEEDS_RELOGIN.value, error=msg, target=target_id
            )

        # (5) публикуем; падение адаптера → FAILED, но не роняет соседей
        try:
            result = await adapter.publish(content, publication_id=publication_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("publish failed %s/%s: %s", channel, target_id, exc)
            await self._repo.set_status(
                publication_id, channel, PublicationStatus.FAILED, target=target_id, error=str(exc)
            )
            # Браузерный шов мог приложить скриншот к исключению (диагностика).
            shot = getattr(exc, "screenshot_path", None)
            return ChannelOutcome(
                channel,
                PublicationStatus.FAILED.value,
                error=str(exc),
                screenshot_path=shot,
                target=target_id,
            )

        # (6) маппим квитанцию → статус
        status = _RESULT_MAP.get(result.status, PublicationStatus.FAILED)
        await self._repo.set_status(
            publication_id,
            channel,
            status,
            target=target_id,
            external_id=result.external_id,
            error=result.error,
        )
        return ChannelOutcome(
            channel,
            status.value,
            external_id=result.external_id,
            error=result.error,
            url=result.url,
            target=target_id,
        )

    async def _existing_outcome(self, publication_id: str, sel: Selection) -> ChannelOutcome:
        row = await self._repo.get_status(publication_id, sel.channel, target=sel.target_id)
        if row is None:
            return ChannelOutcome(sel.channel, PublicationStatus.DONE.value, target=sel.target_id)
        return ChannelOutcome(
            sel.channel,
            str(row.status),
            external_id=row.external_id,
            error=row.error,
            target=sel.target_id,
        )
