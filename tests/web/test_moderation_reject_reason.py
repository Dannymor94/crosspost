"""Tests for reject_reason storage and visibility."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def db_session(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from crosspost.db.engine import create_engine_and_tables

    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"

    async def _make():
        engine = await create_engine_and_tables(db_url)
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    engine, factory = asyncio.run(_make())
    yield factory
    asyncio.run(engine.dispose())


@pytest.fixture()
def profile_id(db_session):
    from crosspost.db.models import Profile

    async def _create():
        async with db_session() as s:
            p = Profile(name="TestProfile")
            s.add(p)
            await s.commit()
            await s.refresh(p)
            return p.id

    return asyncio.run(_create())


def _make_pending(db_session, profile_id):
    from crosspost.db.models import ScheduledPostStatus
    from crosspost.db.publication_repo import PublicationRepository

    async def _run():
        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            post = await repo.create_scheduled(
                content_type="post",
                text="test post",
                title=None,
                media_paths=[],
                channels=["telegram"],
                scheduled_at=datetime.now() + timedelta(hours=1),
                initial_status=ScheduledPostStatus.PENDING_APPROVAL,
            )
            return post.id

    return asyncio.run(_run())


def test_reject_with_reason_stored(db_session, profile_id):
    from crosspost.db.publication_repo import PublicationRepository

    pid = _make_pending(db_session, profile_id)

    async def _run():
        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            ok = await repo.reject_pending(pid, "не подходит по теме")
            assert ok is True
            post = await repo.get_scheduled(pid)
            assert post is not None
            assert post.reject_reason == "не подходит по теме"

    asyncio.run(_run())


def test_reject_without_reason_ok(db_session, profile_id):
    from crosspost.db.publication_repo import PublicationRepository

    pid = _make_pending(db_session, profile_id)

    async def _run():
        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            ok = await repo.reject_pending(pid)
            assert ok is True
            post = await repo.get_scheduled(pid)
            assert post is not None
            assert post.reject_reason is None

    asyncio.run(_run())


def test_client_sees_rejection_reason_in_list(db_session, profile_id):
    """After reject, get_scheduled returns the post with reject_reason set."""
    from crosspost.db.models import ScheduledPostStatus
    from crosspost.db.publication_repo import PublicationRepository

    pid = _make_pending(db_session, profile_id)

    async def _run():
        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            await repo.reject_pending(pid, "причина для клиента")

        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            post = await repo.get_scheduled(pid)
            assert post is not None
            assert post.reject_reason == "причина для клиента"
            assert str(post.status) == str(ScheduledPostStatus.CANCELLED)

    asyncio.run(_run())
