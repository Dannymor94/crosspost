"""PublicationRepository — статусы поканальных публикаций + запланированные посты.

Итерация 2а. Всё ВСЕГДА фильтруется по profile_id (изоляция).

Две области:
  1. Поканальный статус публикации (publications): attempting → done/failed/submitted.
     Ключ идемпотентности — (profile_id, publication_id, channel).
  2. Запланированные посты (scheduled_posts): снимок контента + каналы + время.

Оркестратор пишет статусы сюда; UI поллит list_statuses для «живой» картины.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import (
    Publication,
    PublicationStatus,
    ScheduledPost,
    ScheduledPostStatus,
)


class PublicationRepository:
    """Async-репозиторий публикаций/расписания с изоляцией по profile_id."""

    def __init__(self, session: AsyncSession, *, profile_id: int) -> None:
        self._s = session
        self._pid = profile_id

    # ── Поканальный статус ────────────────────────────────────────────────────

    async def set_status(
        self,
        publication_id: str,
        channel: str,
        status: PublicationStatus,
        *,
        external_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Upsert поканального статуса (profile_id, publication_id, channel)."""
        stmt = (
            sqlite_insert(Publication)
            .values(
                profile_id=self._pid,
                publication_id=publication_id,
                channel=channel,
                status=status,
                external_id=external_id,
                error=error,
            )
            .on_conflict_do_update(
                index_elements=["profile_id", "publication_id", "channel"],
                set_={"status": status, "external_id": external_id, "error": error},
            )
        )
        await self._s.execute(stmt)
        await self._s.commit()

    async def get_status(self, publication_id: str, channel: str) -> Publication | None:
        # populate_existing: upsert идёт сырым SQL мимо identity-map — читаем свежее из БД.
        result = await self._s.execute(
            select(Publication)
            .where(
                Publication.profile_id == self._pid,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def list_statuses(self, publication_id: str) -> list[Publication]:
        result = await self._s.execute(
            select(Publication)
            .where(
                Publication.profile_id == self._pid,
                Publication.publication_id == publication_id,
            )
            .order_by(Publication.channel)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def is_done(self, publication_id: str, channel: str) -> bool:
        row = await self._s.execute(
            select(Publication.id).where(
                Publication.profile_id == self._pid,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
                Publication.status.in_((PublicationStatus.DONE, PublicationStatus.SUBMITTED)),
            )
        )
        return row.first() is not None

    # ── Запланированные посты ─────────────────────────────────────────────────

    async def create_scheduled(
        self,
        *,
        content_type: str,
        text: str,
        title: str | None,
        media_paths: list[str],
        channels: list[str],
        scheduled_at: datetime,
    ) -> ScheduledPost:
        post = ScheduledPost(
            profile_id=self._pid,
            content_type=content_type,
            text=text,
            title=title,
            media_paths=media_paths,
            channels=channels,
            scheduled_at=scheduled_at,
        )
        self._s.add(post)
        await self._s.commit()
        await self._s.refresh(post)
        return post

    async def list_scheduled(self) -> list[ScheduledPost]:
        """Активные (не отменённые) запланированные посты профиля, по времени."""
        result = await self._s.execute(
            select(ScheduledPost)
            .where(
                ScheduledPost.profile_id == self._pid,
                ScheduledPost.status == ScheduledPostStatus.SCHEDULED,
            )
            .order_by(ScheduledPost.scheduled_at)
        )
        return list(result.scalars().all())

    async def get_scheduled(self, scheduled_id: int) -> ScheduledPost | None:
        result = await self._s.execute(
            select(ScheduledPost).where(
                ScheduledPost.id == scheduled_id,
                ScheduledPost.profile_id == self._pid,
            )
        )
        return result.scalar_one_or_none()

    async def cancel_scheduled(self, scheduled_id: int) -> bool:
        """Удалить запланированный пост профиля. True если что-то удалено."""
        result = await self._s.execute(
            delete(ScheduledPost).where(
                ScheduledPost.id == scheduled_id,
                ScheduledPost.profile_id == self._pid,
            )
        )
        await self._s.commit()
        return result.rowcount > 0
