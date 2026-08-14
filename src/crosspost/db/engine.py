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
      • scheduled_posts.link_phrase — текст ссылки на статью (NULL → «Подробнее»);
      • ix_scheduled_report — составной индекс джойна журнала.
      • publications.target — измерение цели в идемпотентности (мультицели):
        SQLite не ALTER-ит UNIQUE, поэтому таблица ПЕРЕСТРАивается (см. ниже).
    Все операции idempotent (guard по table_info / IF NOT EXISTS)."""
    cols = {c["name"] for c in sa.inspect(conn).get_columns("scheduled_posts")}
    if "published_at" not in cols:
        conn.exec_driver_sql("ALTER TABLE scheduled_posts ADD COLUMN published_at DATETIME")
    if "article_text" not in cols:
        conn.exec_driver_sql("ALTER TABLE scheduled_posts ADD COLUMN article_text TEXT")
    if "link_phrase" not in cols:
        conn.exec_driver_sql("ALTER TABLE scheduled_posts ADD COLUMN link_phrase VARCHAR(128)")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_scheduled_report "
        "ON scheduled_posts (profile_id, status, published_at)"
    )
    _migrate_publications_target(conn)


def _migrate_publications_target(conn) -> None:
    """Добавить publications.target + перестроить UNIQUE на (…, channel, target).

    SQLite не умеет ALTER UNIQUE-констрейнта → пересобираем таблицу: создаём
    publications_new с новым constraint, переливаем строки (target=''), удаляем
    старую, переименовываем. Guard по наличию колонки target делает шаг
    идемпотентным и не трогает свежую БД (там create_all уже построил target).
    """
    pcols = {c["name"] for c in sa.inspect(conn).get_columns("publications")}
    if "target" in pcols:
        return
    conn.exec_driver_sql(
        """
        CREATE TABLE publications_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
            publication_id VARCHAR(64) NOT NULL,
            channel VARCHAR(64) NOT NULL,
            target VARCHAR(64) NOT NULL DEFAULT '',
            status VARCHAR(32) NOT NULL,
            external_id VARCHAR(255),
            error TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME,
            CONSTRAINT uq_publication UNIQUE (profile_id, publication_id, channel, target)
        )
        """
    )
    conn.exec_driver_sql(
        "INSERT INTO publications_new "
        "(id, profile_id, publication_id, channel, target, status, external_id, "
        " error, attempt_count, created_at, updated_at) "
        "SELECT id, profile_id, publication_id, channel, '', status, external_id, "
        "       error, attempt_count, created_at, updated_at FROM publications"
    )
    conn.exec_driver_sql("DROP TABLE publications")
    conn.exec_driver_sql("ALTER TABLE publications_new RENAME TO publications")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_publications_publication_id ON publications (publication_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_publications_profile_id ON publications (profile_id)"
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
