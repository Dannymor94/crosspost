"""SchedulerRepository — СИСТЕМНЫЙ доступ к запланированным постам. Трек A, часть 3.

В отличие от PublicationRepository (жёстко scoped по profile_id), это —
доступ УРОВНЯ ДЕМОНА: исполнитель сканирует scheduled_posts ВСЕХ профилей,
чтобы найти те, чьё время настало. Изоляция не нарушается: каждая ПУБЛИКАЦИЯ
затем выполняется строго под profile_id своего поста (см. executor).

Здесь только чтение очереди расписания и перевод поста в терминальный статус —
никаких учёток/сессий (их достаёт profile-scoped код ниже по стеку).
"""

from __future__ import annotations

from datetime import UTC, datetime

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

    async def claim(self, scheduled_id: int) -> bool:
        """Атомарно заклеймить пост под исполнение: SCHEDULED → PUBLISHING.

        Единственная точка входа в обработку. UPDATE ... WHERE status='scheduled'
        сериализуется БД: ровно один клеймящий получает rowcount==1, остальные — 0
        (пост уже взят/отменён/исполнен) и обработку не начинают. Так закрыт гонок
        двух тиков/воркеров на один пост без внешнего лока.
        """
        result = await self._s.execute(
            sql_update(ScheduledPost)
            .where(
                ScheduledPost.id == scheduled_id,
                ScheduledPost.status == ScheduledPostStatus.SCHEDULED,
            )
            .values(status=ScheduledPostStatus.PUBLISHING)
        )
        await self._s.commit()
        return getattr(result, "rowcount", 0) == 1

    async def finalize(self, scheduled_id: int, *, success: bool) -> bool:
        """PUBLISHING → терминал: DONE (все каналы успешны) или FAILED (есть упавший).

        published_at — момент исполнения (монотонный маркер журнала), ставится здесь
        (единственный переход в терминал). Поканальные итоги — в publications.
        """
        status = ScheduledPostStatus.DONE if success else ScheduledPostStatus.FAILED
        result = await self._s.execute(
            sql_update(ScheduledPost)
            .where(
                ScheduledPost.id == scheduled_id,
                ScheduledPost.status == ScheduledPostStatus.PUBLISHING,
            )
            .values(status=status, published_at=datetime.now(UTC))
        )
        await self._s.commit()
        return getattr(result, "rowcount", 0) > 0

    async def release(self, scheduled_id: int) -> bool:
        """PUBLISHING → SCHEDULED: попытки ещё не исчерпаны, добьёт следующий тик."""
        result = await self._s.execute(
            sql_update(ScheduledPost)
            .where(
                ScheduledPost.id == scheduled_id,
                ScheduledPost.status == ScheduledPostStatus.PUBLISHING,
            )
            .values(status=ScheduledPostStatus.SCHEDULED)
        )
        await self._s.commit()
        return getattr(result, "rowcount", 0) > 0

    async def reconcile_stuck(self) -> int:
        """Старт приложения: посты, застрявшие в PUBLISHING (процесс умер в полёте),
        вернуть в SCHEDULED. Идемпотентность по (publication_id, канал, цель) не даст
        дублей при повторной публикации уже успевших каналов. Возвращает число сброшенных.
        """
        result = await self._s.execute(
            sql_update(ScheduledPost)
            .where(ScheduledPost.status == ScheduledPostStatus.PUBLISHING)
            .values(status=ScheduledPostStatus.SCHEDULED)
        )
        await self._s.commit()
        return getattr(result, "rowcount", 0)
