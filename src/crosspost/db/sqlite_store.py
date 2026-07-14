"""SQLiteIdempotencyStore — реализация IdempotencyStore поверх таблицы publications.

Тот же протокол (is_done / mark_done), что у InMemoryIdempotencyStore и JSONIdempotencyStore.
Добавляет get_external_id и increment_attempt (нужны оркестратору, post-MVP).

Все запросы ВСЕГДА фильтруют по profile_id — изоляция между профилями.

Пример использования:
    async with AsyncSession(engine) as session:
        store = SQLiteIdempotencyStore(session, profile_id=1)
        if not await store.is_done(pub_id, "telegram"):
            ...
            await store.mark_done(pub_id, "telegram", external_id="msg:42")
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import Publication, PublicationStatus


class SQLiteIdempotencyStore:
    """IdempotencyStore поверх таблицы publications (async SQLAlchemy)."""

    def __init__(self, session: AsyncSession, *, profile_id: int) -> None:
        self._session = session
        self._profile_id = profile_id

    async def is_done(self, publication_id: str, channel: str) -> bool:
        """True если (profile_id, publication_id, channel) в статусе DONE."""
        row = await self._session.execute(
            select(Publication.id).where(
                Publication.profile_id == self._profile_id,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
                Publication.status == PublicationStatus.DONE,
            )
        )
        return row.first() is not None

    async def mark_done(self, publication_id: str, channel: str, external_id: str | None) -> None:
        """Upsert: создать или обновить запись до статуса DONE с external_id."""
        stmt = (
            sqlite_insert(Publication)
            .values(
                profile_id=self._profile_id,
                publication_id=publication_id,
                channel=channel,
                status=PublicationStatus.DONE,
                external_id=external_id,
            )
            .on_conflict_do_update(
                index_elements=["profile_id", "publication_id", "channel"],
                set_={
                    "status": PublicationStatus.DONE,
                    "external_id": external_id,
                },
            )
        )
        await self._session.execute(stmt)
        await self._session.commit()

    async def get_external_id(self, publication_id: str, channel: str) -> str | None:
        """Вернуть квитанцию по ключу (publication_id, channel, profile_id)."""
        row = await self._session.execute(
            select(Publication.external_id).where(
                Publication.profile_id == self._profile_id,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
            )
        )
        result = row.first()
        return result[0] if result else None

    async def increment_attempt(self, publication_id: str, channel: str) -> int:
        """Увеличить attempt_count на 1, вернуть новое значение."""
        await self._session.execute(
            update(Publication)
            .where(
                Publication.profile_id == self._profile_id,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
            )
            .values(attempt_count=Publication.attempt_count + 1)
        )
        await self._session.commit()
        row = await self._session.execute(
            select(Publication.attempt_count).where(
                Publication.profile_id == self._profile_id,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
            )
        )
        result = row.first()
        return result[0] if result else 0
