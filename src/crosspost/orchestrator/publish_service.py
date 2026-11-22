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
from datetime import UTC, datetime

import httpx
from sqlalchemy import exists, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from crosspost.adapters.base import ChannelAdapter, FailureKind, ResultStatus
from crosspost.channels.validators import VALIDATORS
from crosspost.content.article_link import DEFAULT_LINK_PHRASE, LINK_TEMPLATE, NO_ARTICLE_REASON
from crosspost.content.canonical import CanonicalContent
from crosspost.content.capabilities import supports
from crosspost.content.validation import validate
from crosspost.db.models import Connection, ConnectionState, Credential, PublicationStatus
from crosspost.db.publication_repo import PublicationRepository
from crosspost.orchestrator.article_flow import (
    _ARTICLE_CHANNEL,
    StepResult,
    StepState,
    run_binding,
)
from crosspost.orchestrator.breaker import (
    BreakerPolicy,
    CircuitBreakerRepository,
    retry_after_seconds,
)

logger = logging.getLogger(__name__)

# Статусы, которые считаем успехом канала (для single-shot step связки статьи).
_SUCCESS_STATUSES = {PublicationStatus.DONE.value, PublicationStatus.SUBMITTED.value}

_VERIFY_REASON_ALLOWLIST: frozenset[str] = frozenset({"no_match"})


def _safe_verify_reason(raw: str | None) -> str | None:
    """Persist only fixed verification reason codes, never adapter error contents."""
    return raw if isinstance(raw, str) and raw in _VERIFY_REASON_ALLOWLIST else None


def _error_str(exc: Exception) -> str:
    """Only the category is safe: platform text may echo credentials or signed URLs."""
    return type(exc).__name__


def _failure_signal(exc: Exception, now: datetime) -> tuple[FailureKind, float | None]:
    """Use SDK/HTTP types, including errors while building a connected adapter."""
    from telethon.errors import (
        AuthKeyUnregisteredError,
        FloodWaitError,
        SessionRevokedError,
        UserDeactivatedBanError,
    )

    if isinstance(exc, FloodWaitError):
        return FailureKind.RATE_LIMIT, exc.seconds
    if isinstance(exc, UserDeactivatedBanError):
        return FailureKind.BANNED, None
    if isinstance(exc, (AuthKeyUnregisteredError, SessionRevokedError)):
        return FailureKind.AUTH, None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return FailureKind.RATE_LIMIT, retry_after_seconds(
            exc.response.headers.get("Retry-After"), now=now
        )
    if isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError)):
        return FailureKind.TRANSIENT, None
    return FailureKind.UNKNOWN, None


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


def selections_from_stored(raw) -> list[Selection]:
    """Нормализовать сохранённый channels-JSON в список единиц публикации.

    Толерантно к обоим форматам ScheduledPost.channels:
      • старый  list[str]                       → Selection(ch, "")
      • новый   list[{"channel","target_id"}]   → Selection(ch, str(target_id))
    Так запланированные до милстоуна посты исполняются без миграции данных.
    """
    out: list[Selection] = []
    for item in raw or []:
        if isinstance(item, Selection):
            out.append(item)
        elif isinstance(item, str):
            out.append(Selection(item, ""))
        elif isinstance(item, dict):
            out.append(Selection(item["channel"], str(item.get("target_id") or "")))
        else:  # pragma: no cover — неожиданный тип в JSON
            raise TypeError(f"unrecognized channels item: {item!r}")
    return out


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
    failure_kind: FailureKind = FailureKind.UNKNOWN
    retry_after: float | None = None
    confirmed_done: bool = False


class PublishService:
    def __init__(
        self,
        repo: PublicationRepository,
        adapter_factory: AdapterFactory,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        breaker_policy: BreakerPolicy | None = None,
    ) -> None:
        self._repo = repo
        self._factory = adapter_factory
        self._clock = clock
        self._policy = breaker_policy or BreakerPolicy.from_env()
        self._breaker = CircuitBreakerRepository(repo.session)

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

        deferred: list[ChannelOutcome] = []

        async def _step(sel: Selection, c: CanonicalContent) -> StepResult:
            outcome = await self._publish_one(c, sel, publication_id)
            if outcome.status in {"deferred", "banned", "needs_relogin"}:
                deferred.append(outcome)
                return StepResult(StepState.PENDING, outcome, None)
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
        return flow.outcomes or deferred

    async def retry_channel(
        self,
        content: CanonicalContent,
        channel: str | Selection,
        *,
        publication_id: str,
        before_dispatch: Callable[[], Awaitable[bool]] | None = None,
    ) -> ChannelOutcome:
        """Повторить ОДНУ единицу (канал+цель). Уже done/submitted → не трогаем."""
        sel = _as_selection(channel)
        if await self._repo.is_done(publication_id, sel.channel, target=sel.target_id):
            return await self._existing_outcome(publication_id, sel)
        return await self._publish_one(
            content, sel, publication_id, before_dispatch=before_dispatch
        )

    # ── внутреннее ────────────────────────────────────────────────────────────

    async def _publish_one(
        self,
        content: CanonicalContent,
        sel: Selection,
        publication_id: str,
        *,
        before_dispatch: Callable[[], Awaitable[bool]] | None = None,
    ) -> ChannelOutcome:
        channel, target = sel.channel, sel.target_id
        if await self._repo.is_done(publication_id, channel, target=target):
            return await self._existing_outcome(publication_id, sel)
        session, pid = self._repo.session, self._repo.profile_id
        state = await session.scalar(
            select(Connection.state).where(
                Connection.profile_id == pid, Connection.channel == channel
            )
        )
        if state in (ConnectionState.BANNED, ConnectionState.NEEDS_RELOGIN):
            return ChannelOutcome(channel, str(state), target=target)
        permit = await self._breaker.acquire(pid, channel, now=self._clock(), policy=self._policy)
        if permit is None:
            return ChannelOutcome(channel, "deferred", target=target)
        # The credential snapshot fences connection demotion from an old in-flight send.
        declaration = VALIDATORS.get(channel)
        credential_key = declaration.session_channel or channel if declaration else channel
        credential_predicates = [Credential.profile_id == pid, Credential.channel == credential_key]
        if declaration:
            credential_predicates.append(Credential.kind == declaration.credential_kind)
        snapshot = await session.scalar(select(Credential.blob).where(*credential_predicates))
        if before_dispatch and not await before_dispatch():
            await self._breaker.release_unused(pid, channel, permit, now=self._clock())
            return ChannelOutcome(channel, "deferred", target=target)
        if not await self._repo.authorize_attempt(publication_id, channel, target=target):
            await self._breaker.release_unused(pid, channel, permit, now=self._clock())
            if await self._repo.is_done(publication_id, channel, target=target):
                return await self._existing_outcome(publication_id, sel)
            state = await session.scalar(
                select(Connection.state).where(
                    Connection.profile_id == pid, Connection.channel == channel
                )
            )
            return ChannelOutcome(
                channel,
                str(state)
                if state in (ConnectionState.BANNED, ConnectionState.NEEDS_RELOGIN)
                else "deferred",
                target=target,
            )
        outcome = await self._dispatch_one(content, sel, publication_id)
        await self._breaker.settle(
            pid,
            channel,
            permit,
            now=self._clock(),
            policy=self._policy,
            confirmed_done=outcome.confirmed_done,
            failure=outcome.failure_kind,
            retry_after=outcome.retry_after,
        )
        if outcome.failure_kind in (FailureKind.BANNED, FailureKind.AUTH):
            blocked_state = (
                ConnectionState.BANNED
                if outcome.failure_kind == FailureKind.BANNED
                else ConnectionState.NEEDS_RELOGIN
            )
            credential_matches = (
                exists(
                    select(Credential.id).where(*credential_predicates, Credential.blob == snapshot)
                )
                if snapshot is not None
                else ~exists(select(Credential.id).where(*credential_predicates))
            )
            # Fence INSERT as well as UPDATE: legacy credentials can lack a
            # Connection, but an old result must never block a newer credential.
            await session.execute(
                sqlite_insert(Connection)
                .from_select(
                    ["profile_id", "channel", "state"],
                    select(literal(pid), literal(channel), literal(blocked_state.value)).where(
                        credential_matches
                    ),
                )
                .on_conflict_do_update(
                    index_elements=["profile_id", "channel"],
                    set_={"state": blocked_state},
                    where=Connection.state == ConnectionState.LIVE,
                )
            )
            await session.commit()
        return outcome

    async def _dispatch_one(
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
            failure, retry_after = _failure_signal(exc, self._clock())
            err = _error_str(exc)
            logger.warning("adapter build failed %s/%s: %s", channel, target_id, err)
            await self._repo.set_status(
                publication_id,
                channel,
                PublicationStatus.FAILED,
                target=target_id,
                error=f"сборка канала: {err}",
            )
            return ChannelOutcome(
                channel,
                PublicationStatus.FAILED.value,
                error=f"сборка канала: {err}",
                target=target_id,
                failure_kind=failure,
                retry_after=retry_after,
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
            failure, retry_after = _failure_signal(exc, self._clock())
            err = _error_str(exc)
            logger.warning("publish failed %s/%s: %s", channel, target_id, err)
            await self._repo.set_status(
                publication_id, channel, PublicationStatus.FAILED, target=target_id, error=err
            )
            # Браузерный шов мог приложить скриншот к исключению (диагностика).
            shot = getattr(exc, "screenshot_path", None)
            return ChannelOutcome(
                channel,
                PublicationStatus.FAILED.value,
                error=err,
                screenshot_path=shot,
                target=target_id,
                failure_kind=failure,
                retry_after=retry_after,
            )

        # (6) маппим квитанцию → статус
        status = _RESULT_MAP.get(result.status, PublicationStatus.FAILED)
        receipt = (
            result.external_id
            if status in (PublicationStatus.DONE, PublicationStatus.SUBMITTED)
            else None
        )
        url = (
            result.url if status in (PublicationStatus.DONE, PublicationStatus.SUBMITTED) else None
        )
        # Second line of defence: redact any signed media token an adapter's error
        # might carry before it reaches the DB or the API response.
        if status == PublicationStatus.SUBMITTED:
            safe_error = _safe_verify_reason(result.error)
        else:
            safe_error = f"Ошибка площадки ({result.failure_kind.value})" if result.error else None
        await self._repo.set_status(
            publication_id,
            channel,
            status,
            target=target_id,
            external_id=receipt,
            error=safe_error,
        )
        return ChannelOutcome(
            channel,
            status.value,
            external_id=receipt,
            error=safe_error,
            url=url,
            target=target_id,
            failure_kind=FailureKind.AUTH
            if result.status == ResultStatus.NEEDS_RELOGIN
            else result.failure_kind,
            retry_after=result.retry_after,
            confirmed_done=result.status == ResultStatus.DONE and bool(result.external_id),
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
