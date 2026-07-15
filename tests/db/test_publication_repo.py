"""Тесты PublicationRepository: статусы, запланированные посты, изоляция. Итерация 2а."""

from __future__ import annotations

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
        content_type="post", text="x", title=None, media_paths=[],
        channels=["telegram"], scheduled_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert await repo.cancel_scheduled(post.id) is True
    assert await repo.list_scheduled() == []
    # Повторная отмена — no-op.
    assert await repo.cancel_scheduled(post.id) is False


async def test_scheduled_isolated_between_profiles(session):
    repo_a = _repo(session, pid=1)
    repo_b = _repo(session, pid=2)
    post = await repo_a.create_scheduled(
        content_type="post", text="A", title=None, media_paths=[],
        channels=["telegram"], scheduled_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    # B не видит и не может отменить пост A.
    assert await repo_b.list_scheduled() == []
    assert await repo_b.cancel_scheduled(post.id) is False
    assert len(await repo_a.list_scheduled()) == 1
