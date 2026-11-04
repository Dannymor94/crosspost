"""Фабрика async-движка; схема обновляется только ревизиями Alembic."""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from crosspost.db.migration_runner import upgrade_database

_DEFAULT_DB_URL = "sqlite+aiosqlite:///runtime/db.sqlite3"


def get_db_url() -> str:
    return os.environ.get("DB_URL", _DEFAULT_DB_URL)


async def create_engine_and_tables(url: str | None = None) -> AsyncEngine:
    """Создать движок и выполнить Alembic upgrade head до обслуживания запросов."""
    engine = create_async_engine(url or get_db_url(), echo=False)
    try:
        async with engine.begin() as conn:
            if engine.dialect.name == "sqlite":
                # Явная транзакция включает DDL; сериализует параллельные старты.
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
            await conn.run_sync(upgrade_database)
    except BaseException:
        await engine.dispose()
        raise
    return engine
