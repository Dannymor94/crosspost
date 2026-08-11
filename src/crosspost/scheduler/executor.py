"""Исполнитель расписания — ядро милстоуна. Трек A, часть 3.

Фоновый цикл: находит scheduled-посты, чьё время настало, и публикует их
через существующий PublishService под profile_id КАЖДОГО поста (изоляция).

Инварианты (CLAUDE.md):
  • Идемпотентность через ВНУТРЕННИЙ ключ. publication_id детерминирован —
    f"schedpost-{post.id}". Он стабилен между перезапусками, поэтому канал,
    уже отмеченный done в publications, второй раз не публикуется — даже если
    процесс упал и поднялся. external_id тут ни при чём.
  • Пейсинг: несколько «созревших» постов публикуются ПООЧЕРЁДНО с паузой,
    не залпом. Внутри поста каналы и так идут последовательно (PublishService).
    Один браузер за раз → устойчивость и анти-бан важнее скорости.
  • Ретрай по политике: упавший канал повторяется до SCHEDULER_MAX_ATTEMPTS
    попыток (счётчик — publications.attempt_count, переживает рестарт), затем
    остаётся failed + запись в logs. Забаненный/needs_relogin канал тоже
    упирается в потолок и не крутится вечно.
  • Пост считается исполненным (status=published, «не брать снова»), когда ВСЕ
    его каналы терминальны: done/submitted ИЛИ исчерпали попытки. Тогда же —
    чистка медиа (каталог sched-<uuid>). Частичный успех — норма.

Планировщик — демон УРОВНЯ СИСТЕМЫ: сканирует очередь всех профилей
(SchedulerRepository), но каждую отправку исполняет строго под profile_id поста.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.db.log_repo import LogRepository
from crosspost.db.models import Publication, PublicationStatus, ScheduledPost
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.scheduler_repo import SchedulerRepository
from crosspost.db.vault import get_vault
from crosspost.orchestrator.adapter_factory import build_profile_adapter
from crosspost.orchestrator.publish_service import ChannelOutcome, PublishService
from crosspost.web import media as media_store

logger = logging.getLogger(__name__)

# Терминальный успех: канал доставлен (или ушёл на модерацию) — не ретраим.
_SUCCESS = {PublicationStatus.DONE.value, PublicationStatus.SUBMITTED.value}

SleepFn = Callable[[float], Awaitable[None]]


def scheduled_publication_id(post_id: int) -> str:
    """Детерминированный внутренний ключ поста — стабилен между перезапусками."""
    return f"schedpost-{post_id}"


def _as_naive(dt: datetime) -> datetime:
    """Снять tzinfo для сравнения времён (в очереди возможны naive и aware вперемешку)."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


class ScheduledExecutor:
    """Периодически публикует созревшие запланированные посты."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_attempts: int = 3,
        pacing_seconds: float = 5.0,
        poll_seconds: float = 30.0,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._factory = session_factory
        self._max_attempts = max(1, max_attempts)
        self._pacing = max(0.0, pacing_seconds)
        self._poll = max(0.0, poll_seconds)
        self._sleep = sleep

    # ── Конфигурация ──────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, session_factory: async_sessionmaker[AsyncSession]) -> ScheduledExecutor:
        """Построить исполнитель из env (значения инжектит load_config в lifespan)."""
        return cls(
            session_factory,
            max_attempts=int(os.environ.get("SCHEDULER_MAX_ATTEMPTS", "3")),
            pacing_seconds=float(os.environ.get("SCHEDULER_PACING_SECONDS", "5")),
            poll_seconds=float(os.environ.get("SCHEDULER_POLL_SECONDS", "30")),
        )

    # ── Публичный API ─────────────────────────────────────────────────────────

    async def publish_post_now(self, post_id: int) -> None:
        """Немедленно опубликовать запланированный пост, не дожидаясь тика.

        Идемпотентно: если пост уже исполнен или отменён — ничего не делает.
        """
        await self._process_post(post_id)

    async def run_forever(self) -> None:
        """Бесконечный цикл поллинга. Отменяется через task.cancel() в lifespan."""
        logger.info(
            "scheduler executor started (poll=%ss pacing=%ss max_attempts=%s)",
            self._poll,
            self._pacing,
            self._max_attempts,
        )
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                logger.info("scheduler executor stopped")
                raise
            except Exception:  # noqa: BLE001 — тик не должен ронять демона
                logger.exception("scheduler tick failed")
            await self._sleep(self._poll)

    async def run_once(self, *, now: datetime | None = None) -> list[int]:
        """Один проход: обработать все созревшие посты ПООЧЕРЁДНО с пейсингом.

        Возвращает id обработанных постов (для тестов/диагностики).
        """
        moment = now if now is not None else datetime.now()
        async with self._factory() as session:
            active = await SchedulerRepository(session).list_scheduled_active()
        due_ids = [p.id for p in active if _as_naive(p.scheduled_at) <= _as_naive(moment)]

        processed: list[int] = []
        for i, post_id in enumerate(due_ids):
            if i > 0 and self._pacing > 0:
                await self._sleep(self._pacing)  # разнос отправок (анти-бан)
            await self._process_post(post_id)
            processed.append(post_id)
        return processed

    # ── Обработка одного поста ────────────────────────────────────────────────

    async def _process_post(self, post_id: int) -> None:
        async with self._factory() as session:
            sched_repo = SchedulerRepository(session)
            post = await sched_repo.get(post_id)
            if post is None or str(post.status) != "scheduled":
                return  # отменён/уже исполнен между сканом и обработкой

            channels = list(post.channels or [])
            if not channels:
                await sched_repo.mark_published(post_id)  # нечего слать → закрыть
                return

            pub_repo = PublicationRepository(session, profile_id=post.profile_id)
            pubid = scheduled_publication_id(post_id)
            existing = {r.channel: r for r in await pub_repo.list_statuses(pubid)}
            pending = [ch for ch in channels if self._is_pending(existing.get(ch))]

            if pending:
                await self._attempt_channels(session, post, pub_repo, pubid, pending)

            # Пере-оценка после попыток: все ли каналы терминальны?
            rows = {r.channel: r for r in await pub_repo.list_statuses(pubid)}
            if self._all_terminal(channels, rows):
                await sched_repo.mark_published(post_id)
                self._cleanup_media(post.media_paths)

    async def _attempt_channels(
        self,
        session: AsyncSession,
        post: ScheduledPost,
        pub_repo: PublicationRepository,
        pubid: str,
        pending: list[str],
    ) -> None:
        content = self._content_from_post(post)
        profile_repo = ProfileRepository(session, vault=get_vault())
        service = self._make_service(profile_repo, pub_repo, post.profile_id)
        log_repo = LogRepository(session)

        for channel in pending:
            # Счётчик +1 ДО попытки — переживает падение процесса (потолок соблюдён).
            await pub_repo.bump_attempt(pubid, channel)
            outcome = await service.retry_channel(content, channel, publication_id=pubid)
            await self._log_outcome(log_repo, post.profile_id, channel, pubid, outcome)

    # ── Диагностика (Блок 4) ──────────────────────────────────────────────────

    async def _log_outcome(
        self,
        log_repo: LogRepository,
        profile_id: int,
        channel: str,
        pubid: str,
        outcome: ChannelOutcome,
    ) -> None:
        ok = outcome.status in _SUCCESS
        level = "INFO" if ok else "ERROR"
        msg = f"scheduler: {channel} → {outcome.status}"
        if outcome.external_id:
            msg += f" ({outcome.external_id})"
        if outcome.error:
            msg += f" — {outcome.error}"
        await log_repo.add(
            profile_id,
            channel=channel,
            publication_id=pubid,
            level=level,
            message=msg,
            screenshot_path=outcome.screenshot_path,
        )

    # ── Хелперы ───────────────────────────────────────────────────────────────

    def _make_service(
        self,
        profile_repo: ProfileRepository,
        pub_repo: PublicationRepository,
        profile_id: int,
    ) -> PublishService:
        async def factory(channel: str):
            return await build_profile_adapter(profile_repo, profile_id, channel)

        return PublishService(pub_repo, factory)

    def _content_from_post(self, post: ScheduledPost) -> CanonicalContent:
        return CanonicalContent(
            type=ContentType(post.content_type),
            text=post.text or "",
            title=post.title,
            media_paths=[Path(p) for p in (post.media_paths or [])],
        )

    def _is_pending(self, row: Publication | None) -> bool:
        """Нужна ли каналу ещё одна попытка."""
        if row is None:
            return True  # ни разу не пробовали
        if str(row.status) in _SUCCESS:
            return False  # доставлено
        return row.attempt_count < self._max_attempts  # упал, но лимит не исчерпан

    def _all_terminal(self, channels: list[str], rows: dict[str, Publication]) -> bool:
        """Все каналы финализированы: успех ИЛИ исчерпан ретрай."""
        for ch in channels:
            row = rows.get(ch)
            if row is None:
                return False
            if str(row.status) in _SUCCESS:
                continue
            if row.attempt_count >= self._max_attempts:
                continue  # терминальный провал
            return False
        return True

    def _cleanup_media(self, media_paths: list) -> None:
        """Удалить каталоги медиа поста (sched-<uuid>) — все каналы финализированы."""
        keys = {Path(p).parent.name for p in (media_paths or [])}
        for key in keys:
            media_store.cleanup_media(key)
