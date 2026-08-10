"""SchedulerRepository — СИСТЕМНЫЙ доступ к запланированным постам. Трек A, часть 3.

В отличие от PublicationRepository (жёстко scoped по profile_id), это —
доступ УРОВНЯ ДЕМОНА: исполнитель сканирует scheduled_posts ВСЕХ профилей,
чтобы найти те, чьё время настало. Изоляция не нарушается: каждая ПУБЛИКАЦИЯ
затем выполняется строго под profile_id своего поста (см. executor).

Здесь только чтение очереди расписания и перевод поста в терминальный статус —
никаких учёток/сессий (их достаёт profile-scoped код ниже по стеку).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import ScheduledPost, ScheduledPostStatus


class SchedulerRepository:
    """Системный (кросс-профильный) доступ к очереди расписания."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list_scheduled_active(self) -> list[ScheduledPost]:
        """Все НЕисполненные посты (status=scheduled) всех профилей, по времени.

        Фильтр «время настало» делает исполнитель в Python — так надёжнее
        сравнения naive/aware datetime, чем строковое сравнение в SQLite.
        Постов в очереди немного (единицы-десятки), полный скан дёшев.
        """
        result = await self._s.execute(
            select(ScheduledPost)
            .where(ScheduledPost.status == ScheduledPostStatus.SCHEDULED)
            .order_by(ScheduledPost.scheduled_at)
        )
        return list(result.scalars().all())

    async def get(self, scheduled_id: int) -> ScheduledPost | None:
        """Пост по id (любого профиля) — свежий из БД, мимо identity-map."""
        result = await self._s.execute(
            select(ScheduledPost)
            .where(ScheduledPost.id == scheduled_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def mark_published(self, scheduled_id: int) -> bool:
        """Перевести пост в терминальный статус published (исполнен планировщиком).

        «Исполнен» = все каналы финализировались (успех ИЛИ исчерпан ретрай).
        Поканальные итоги живут в publications; здесь только флаг «не брать снова».
        """
        result = await self._s.execute(
            sql_update(ScheduledPost)
            .where(ScheduledPost.id == scheduled_id)
            .values(status=ScheduledPostStatus.PUBLISHED)
        )
        await self._s.commit()
        return getattr(result, "rowcount", 0) > 0
