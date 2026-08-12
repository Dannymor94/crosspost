"""Фабрика async-движка SQLAlchemy. Слой 0.1.

Использование:
    engine = await create_engine_and_tables("sqlite+aiosqlite:///runtime/db.sqlite3")
    async with AsyncSession(engine) as session:
        ...

URL по умолчанию берётся из env DB_URL (runtime/db.sqlite3 если не задан).
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from crosspost.db.models import Base

_DEFAULT_DB_URL = "sqlite+aiosqlite:///runtime/db.sqlite3"


def get_db_url() -> str:
    return os.environ.get("DB_URL", _DEFAULT_DB_URL)


def _migrate_sqlite(conn) -> None:
    """Лёгкие ин-плейс миграции для SQLite (Alembic в проекте нет).

    create_all не ALTER-ит существующие таблицы: у уже созданной БД не появятся
    ни новая колонка, ни новый индекс. Здесь идемпотентно доводим схему:
      • scheduled_posts.published_at — колонка добавлена позже (маркер исполнения);
      • scheduled_posts.article_text — тело лонгрида для связки Telegraph→канал;
      • ix_scheduled_report — составной индекс джойна журнала.
    Все операции idempotent (guard по table_info / IF NOT EXISTS)."""
    cols = {c["name"] for c in sa.inspect(conn).get_columns("scheduled_posts")}
    if "published_at" not in cols:
        conn.exec_driver_sql("ALTER TABLE scheduled_posts ADD COLUMN published_at DATETIME")
    if "article_text" not in cols:
        conn.exec_driver_sql("ALTER TABLE scheduled_posts ADD COLUMN article_text TEXT")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_scheduled_report "
        "ON scheduled_posts (profile_id, status, published_at)"
    )


async def create_engine_and_tables(url: str | None = None) -> AsyncEngine:
    """Создать движок и применить схему (CREATE TABLE IF NOT EXISTS + миграции).

    Для тестов передавай url="sqlite+aiosqlite:///:memory:".
    Для продакшна оставь url=None — возьмёт из DB_URL / дефолт.
    """
    resolved = url or get_db_url()
    engine = create_async_engine(resolved, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if engine.dialect.name == "sqlite":
            await conn.run_sync(_migrate_sqlite)
    return engine
