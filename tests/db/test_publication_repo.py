"""Тесты PublicationRepository: статусы, запланированные посты, изоляция. Итерация 2а."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import PublicationStatus
from crosspost.db.publication_repo import PublicationRepository


@pytest_asyncio.fixture
async def session():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


def _repo(session, pid=1):
    return PublicationRepository(session, profile_id=pid)


async def test_set_and_get_status(session):
    repo = _repo(session)
    await repo.set_status("p1", "telegram", PublicationStatus.ATTEMPTING)
    row = await repo.get_status("p1", "telegram")
    assert str(row.status) == "attempting"

    await repo.set_status("p1", "telegram", PublicationStatus.DONE, external_id="tg1")
    row = await repo.get_status("p1", "telegram")
    assert str(row.status) == "done"
    assert row.external_id == "tg1"


async def test_is_done_covers_submitted(session):
    repo = _repo(session)
    await repo.set_status("p1", "yandex", PublicationStatus.SUBMITTED, external_id="y1")
    assert await repo.is_done("p1", "yandex") is True


async def test_list_statuses_scoped_to_publication(session):
    repo = _repo(session)
    await repo.set_status("p1", "telegram", PublicationStatus.DONE)
    await repo.set_status("p1", "vk_wall", PublicationStatus.FAILED, error="oops")
    await repo.set_status("p2", "telegram", PublicationStatus.DONE)

    got = {s.channel: str(s.status) for s in await repo.list_statuses("p1")}
    assert got == {"telegram": "done", "vk_wall": "failed"}


# ── Измерение цели (мультицели) ───────────────────────────────────────────────


async def test_targets_of_same_channel_do_not_collide(session):
    """Две цели одного канала под одним publication_id — независимые строки."""
    repo = _repo(session)
    await repo.set_status("p1", "vk_wall", PublicationStatus.DONE, target="7", external_id="a")
    await repo.set_status("p1", "vk_wall", PublicationStatus.FAILED, target="9", error="boom")

    ok = await repo.get_status("p1", "vk_wall", target="7")
    bad = await repo.get_status("p1", "vk_wall", target="9")
    assert str(ok.status) == "done"
    assert ok.external_id == "a"
    assert str(bad.status) == "failed"
    assert bad.error == "boom"
    assert len(await repo.list_statuses("p1")) == 2


async def test_is_done_is_per_target(session):
    repo = _repo(session)
    await repo.set_status("p1", "vk_wall", PublicationStatus.DONE, target="7")
    assert await repo.is_done("p1", "vk_wall", target="7") is True
    assert await repo.is_done("p1", "vk_wall", target="9") is False


async def test_bump_attempt_is_per_target(session):
    repo = _repo(session)
    await repo.bump_attempt("p1", "vk_wall", target="7")
    await repo.bump_attempt("p1", "vk_wall", target="7")
    await repo.bump_attempt("p1", "vk_wall", target="9")

    a = await repo.get_status("p1", "vk_wall", target="7")
    b = await repo.get_status("p1", "vk_wall", target="9")
    assert a.attempt_count == 2
    assert b.attempt_count == 1


async def test_empty_target_default_isolated_from_named(session):
    """Таргетлесс-строка ("") и именованная цель того же канала не пересекаются."""
    repo = _repo(session)
    await repo.set_status("p1", "telegram", PublicationStatus.DONE)  # target=""
    await repo.set_status("p1", "telegram", PublicationStatus.FAILED, target="5")
    assert await repo.is_done("p1", "telegram") is True
    assert await repo.is_done("p1", "telegram", target="5") is False


# ── Scheduled posts ───────────────────────────────────────────────────────────


async def test_create_and_list_scheduled(session):
    repo = _repo(session)
    when = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    post = await repo.create_scheduled(
        content_type="post",
        text="позже",
        title=None,
        media_paths=["runtime/tmp/x.jpg"],
        channels=["telegram", "vk_wall"],
        scheduled_at=when,
    )
    assert post.id is not None

    listed = await repo.list_scheduled()
    assert len(listed) == 1
    assert listed[0].channels == ["telegram", "vk_wall"]
    assert listed[0].text == "позже"


async def test_cancel_scheduled_removes_it(session):
    repo = _repo(session)
    post = await repo.create_scheduled(
        content_type="post",
        text="x",
        title=None,
        media_paths=[],
        channels=["telegram"],
        scheduled_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert await repo.cancel_scheduled(post.id) is True
    assert await repo.list_scheduled() == []
    # Повторная отмена — no-op.
    assert await repo.cancel_scheduled(post.id) is False


async def test_scheduled_isolated_between_profiles(session):
    repo_a = _repo(session, pid=1)
    repo_b = _repo(session, pid=2)
    post = await repo_a.create_scheduled(
        content_type="post",
        text="A",
        title=None,
        media_paths=[],
        channels=["telegram"],
        scheduled_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    # B не видит и не может отменить пост A.
    assert await repo_b.list_scheduled() == []
    assert await repo_b.cancel_scheduled(post.id) is False
    assert len(await repo_a.list_scheduled()) == 1


# ── Rebuild-миграция старой схемы publications ────────────────────────────────


async def test_migration_rebuilds_old_publications_preserving_rows(tmp_path):
    """Старая БД без колонки target: миграция переливает строки с target='',
    новый UNIQUE позволяет двум целям канала сосуществовать."""
    db_file = tmp_path / "legacy.sqlite3"
    # Старая схема publications (до мультицелей): UNIQUE без target.
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE profiles (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(128) NOT NULL,
            created_at DATETIME
        );
        CREATE TABLE publications (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
            publication_id VARCHAR(64) NOT NULL,
            channel VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            external_id VARCHAR(255),
            error TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME,
            CONSTRAINT uq_publication UNIQUE (profile_id, publication_id, channel)
        );
        INSERT INTO profiles (id, name) VALUES (1, 'p');
        INSERT INTO publications
            (profile_id, publication_id, channel, status, external_id, attempt_count)
        VALUES (1, 'old1', 'telegram', 'done', 'ext-1', 3);
        """
    )
    con.commit()
    con.close()

    engine = await create_engine_and_tables(f"sqlite+aiosqlite:///{db_file}")
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            repo = PublicationRepository(s, profile_id=1)
            old = await repo.get_status("old1", "telegram")
            assert old is not None
            assert old.target == ""
            assert old.external_id == "ext-1"
            assert old.attempt_count == 3
            # Новое измерение цели работает: две цели того же канала не коллизят.
            await repo.set_status("old1", "telegram", PublicationStatus.DONE, target="42")
            assert len(await repo.list_statuses("old1")) == 2
    finally:
        await engine.dispose()


async def test_migration_is_idempotent_on_fresh_db(tmp_path):
    """Повторный прогон create_engine_and_tables на уже мигрированной БД не падает."""
    db_file = tmp_path / "fresh.sqlite3"
    url = f"sqlite+aiosqlite:///{db_file}"

    e1 = await create_engine_and_tables(url)
    await e1.dispose()
    e2 = await create_engine_and_tables(url)  # второй прогон — миграция уже не нужна
    await e2.dispose()

    # target уже есть → rebuild пропущен, схема консистентна.
    con = sqlite3.connect(db_file)
    cols = {r[1] for r in con.execute("PRAGMA table_info(publications)")}
    con.close()
    assert "target" in cols
