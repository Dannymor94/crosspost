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

    async def is_done(self, publication_id: str, channel: str, *, target: str = "") -> bool:
        """True если (profile_id, publication_id, channel, target) в статусе DONE."""
        row = await self._session.execute(
            select(Publication.id).where(
                Publication.profile_id == self._profile_id,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
                Publication.target == target,
                Publication.status == PublicationStatus.DONE,
            )
        )
        return row.first() is not None

    async def mark_done(
        self, publication_id: str, channel: str, external_id: str | None, *, target: str = ""
    ) -> None:
        """Upsert: создать или обновить запись до статуса DONE с external_id."""
        stmt = (
            sqlite_insert(Publication)
            .values(
                profile_id=self._profile_id,
                publication_id=publication_id,
                channel=channel,
                target=target,
                status=PublicationStatus.DONE,
                external_id=external_id,
            )
            .on_conflict_do_update(
                index_elements=["profile_id", "publication_id", "channel", "target"],
                set_={
                    "status": PublicationStatus.DONE,
                    "external_id": external_id,
                },
            )
        )
        await self._session.execute(stmt)
        await self._session.commit()

    async def get_external_id(
        self, publication_id: str, channel: str, *, target: str = ""
    ) -> str | None:
        """Вернуть квитанцию по ключу (profile_id, publication_id, channel, target)."""
        row = await self._session.execute(
            select(Publication.external_id).where(
                Publication.profile_id == self._profile_id,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
                Publication.target == target,
            )
        )
        result = row.first()
        return result[0] if result else None

    async def increment_attempt(
        self, publication_id: str, channel: str, *, target: str = ""
    ) -> int:
        """Увеличить attempt_count на 1, вернуть новое значение."""
        await self._session.execute(
            update(Publication)
            .where(
                Publication.profile_id == self._profile_id,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
                Publication.target == target,
            )
            .values(attempt_count=Publication.attempt_count + 1)
        )
        await self._session.commit()
        row = await self._session.execute(
            select(Publication.attempt_count).where(
                Publication.profile_id == self._profile_id,
                Publication.publication_id == publication_id,
                Publication.channel == channel,
                Publication.target == target,
            )
        )
        result = row.first()
        return result[0] if result else 0


class SyncSQLiteIdempotencyStore:
    """Sync IdempotencyStore backed by a SQLite checkpoints table.

    Adapter checkpoints (Telegram #photo, Instagram #container) are written
    synchronously — adapters call the store without await. This class uses the
    stdlib sqlite3 module and writes to a dedicated 'checkpoints' table in the
    same DB file used by the async stack. Schema is created lazily on init.

    Profile isolation: every row is scoped to profile_id, consistent with
    ProfileRepository rules.
    """

    _CREATE = """
        CREATE TABLE IF NOT EXISTS checkpoints (
            profile_id  INTEGER NOT NULL,
            publication_id TEXT NOT NULL,
            channel        TEXT NOT NULL,
            target         TEXT NOT NULL DEFAULT '',
            external_id    TEXT,
            PRIMARY KEY (profile_id, publication_id, channel, target)
        )
    """

    def __init__(self, db_path: str, *, profile_id: int) -> None:
        import sqlite3

        self._db_path = db_path
        self._profile_id = profile_id
        self._sqlite3 = sqlite3
        with self._connect() as con:
            con.execute(self._CREATE)

    def _connect(self):
        """Open a connection with WAL mode and an explicit busy-wait timeout.

        timeout=5.0 — when the DB is locked by a concurrent writer (async engine
        or another sync connection), retry for up to 5 s before raising.  WAL lets
        readers proceed concurrently but writers still compete; the timeout is
        what prevents an immediate OperationalError under contention.
        """
        con = self._sqlite3.connect(self._db_path, timeout=5.0)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def is_done(self, publication_id: str, channel: str, *, target: str = "") -> bool:
        con = self._connect()
        try:
            with con:
                row = con.execute(
                    "SELECT 1 FROM checkpoints WHERE profile_id=? AND publication_id=? "
                    "AND channel=? AND target=?",
                    (self._profile_id, publication_id, channel, target),
                ).fetchone()
        finally:
            con.close()
        return row is not None

    def mark_done(
        self,
        publication_id: str,
        channel: str,
        external_id: str | None,
        *,
        target: str = "",
    ) -> None:
        con = self._connect()
        try:
            with con:
                con.execute(
                    "INSERT OR REPLACE INTO checkpoints "
                    "(profile_id, publication_id, channel, target, external_id) "
                    "VALUES (?,?,?,?,?)",
                    (self._profile_id, publication_id, channel, target, external_id),
                )
        finally:
            con.close()

    def get_external_id(self, publication_id: str, channel: str, *, target: str = "") -> str | None:
        con = self._connect()
        try:
            with con:
                row = con.execute(
                    "SELECT external_id FROM checkpoints WHERE profile_id=? "
                    "AND publication_id=? AND channel=? AND target=?",
                    (self._profile_id, publication_id, channel, target),
                ).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    def clear_done(self, publication_id: str, channel: str, *, target: str = "") -> None:
        """Remove a specific checkpoint row so the next attempt treats it as fresh."""
        con = self._connect()
        try:
            with con:
                con.execute(
                    "DELETE FROM checkpoints WHERE profile_id=? AND publication_id=? "
                    "AND channel=? AND target=?",
                    (self._profile_id, publication_id, channel, target),
                )
        finally:
            con.close()

    def delete_checkpoints(self, publication_id: str) -> None:
        """Remove intermediate step-keys (pub_id#step) once publication is terminal.

        Never removes the terminal row itself (bare publication_id without #).
        """
        con = self._connect()
        try:
            with con:
                con.execute(
                    "DELETE FROM checkpoints WHERE profile_id=? AND publication_id LIKE ?",
                    (self._profile_id, f"{publication_id}#%"),
                )
        finally:
            con.close()


def sync_checkpoint_store(profile_id: int) -> SyncSQLiteIdempotencyStore:
    """Create a SyncSQLiteIdempotencyStore for the configured DB file.

    Convenience factory used by publish_service and executor to clean up
    checkpoints after a publication reaches a terminal state.
    """
    from crosspost.db.engine import get_db_url

    db_path = get_db_url().removeprefix("sqlite+aiosqlite:///")
    return SyncSQLiteIdempotencyStore(db_path, profile_id=profile_id)
