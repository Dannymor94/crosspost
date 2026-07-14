"""Фабрика async-движка SQLAlchemy. Слой 0.1.

Использование:
    engine = await create_engine_and_tables("sqlite+aiosqlite:///runtime/db.sqlite3")
    async with AsyncSession(engine) as session:
        ...

URL по умолчанию берётся из env DB_URL (runtime/db.sqlite3 если не задан).
"""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from crosspost.db.models import Base

_DEFAULT_DB_URL = "sqlite+aiosqlite:///runtime/db.sqlite3"


def get_db_url() -> str:
    return os.environ.get("DB_URL", _DEFAULT_DB_URL)


async def create_engine_and_tables(url: str | None = None) -> AsyncEngine:
    """Создать движок и применить схему (CREATE TABLE IF NOT EXISTS).

    Для тестов передавай url="sqlite+aiosqlite:///:memory:".
    Для продакшна оставь url=None — возьмёт из DB_URL / дефолт.
    """
    resolved = url or get_db_url()
    engine = create_async_engine(resolved, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine
