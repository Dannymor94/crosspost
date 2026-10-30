"""Tests for PENDING_APPROVAL flow: executor never picks up, approve/reject CAS."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def db_session(tmp_path):
    """Yield an async session to a fresh in-memory-ish SQLite DB."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from crosspost.db.engine import create_engine_and_tables

    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"

    async def _make():
        engine = await create_engine_and_tables(db_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return engine, factory

    engine, factory = asyncio.run(_make())
    yield factory
    asyncio.run(engine.dispose())


@pytest.fixture()
def profile_id(db_session):
    """Create a test profile and return its id."""
    from crosspost.db.models import Profile

    async def _create():
        async with db_session() as s:
            p = Profile(name="TestProfile")
            s.add(p)
            await s.commit()
            await s.refresh(p)
            return p.id

    return asyncio.run(_create())


def _make_repo(db_session, profile_id):
    from crosspost.db.publication_repo import PublicationRepository

    # Return a factory that creates a new repo per session
    async def _ctx():
        async with db_session() as s:
            return s, PublicationRepository(s, profile_id=profile_id)

    return _ctx


# ── Pending approval never executed ───────────────────────────────────────────


def test_pending_approval_not_picked_up_by_scheduler(db_session, profile_id):
    """PENDING_APPROVAL posts must not appear in list_scheduled_active (executor query)."""
    from crosspost.db.models import ScheduledPostStatus
    from crosspost.db.publication_repo import PublicationRepository
    from crosspost.db.scheduler_repo import SchedulerRepository

    async def _run():
        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            post = await repo.create_scheduled(
                content_type="post",
                text="pending test",
                title=None,
                media_paths=[],
                channels=["telegram"],
                scheduled_at=datetime.now() - timedelta(minutes=1),
                initial_status=ScheduledPostStatus.PENDING_APPROVAL,
            )
            assert post.status == ScheduledPostStatus.PENDING_APPROVAL

        # Executor uses SchedulerRepository (cross-profile) — must not see PENDING_APPROVAL
        async with db_session() as s:
            sched_repo = SchedulerRepository(s)
            active = await sched_repo.list_scheduled_active()
            assert all(p.status != ScheduledPostStatus.PENDING_APPROVAL for p in active)

    asyncio.run(_run())


# ── Approve CAS ───────────────────────────────────────────────────────────────


def test_approve_moves_to_scheduled(db_session, profile_id):
    """approve_pending: PENDING_APPROVAL → SCHEDULED atomically."""
    from crosspost.db.models import ScheduledPostStatus
    from crosspost.db.publication_repo import PublicationRepository

    async def _run():
        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            post = await repo.create_scheduled(
                content_type="post",
                text="to approve",
                title=None,
                media_paths=[],
                channels=["telegram"],
                scheduled_at=datetime.now() + timedelta(hours=1),
                initial_status=ScheduledPostStatus.PENDING_APPROVAL,
            )
            pid = post.id

        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            ok = await repo.approve_pending(pid)
            assert ok is True
            post = await repo.get_scheduled(pid)
            assert post is not None
            assert post.status == ScheduledPostStatus.SCHEDULED

    asyncio.run(_run())


def test_approve_idempotency_fails_second_time(db_session, profile_id):
    """Second approve_pending call returns False (post is no longer PENDING_APPROVAL)."""
    from crosspost.db.models import ScheduledPostStatus
    from crosspost.db.publication_repo import PublicationRepository

    async def _run():
        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            post = await repo.create_scheduled(
                content_type="post",
                text="double approve",
                title=None,
                media_paths=[],
                channels=["telegram"],
                scheduled_at=datetime.now() + timedelta(hours=1),
                initial_status=ScheduledPostStatus.PENDING_APPROVAL,
            )
            pid = post.id

        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            first = await repo.approve_pending(pid)
            assert first is True

        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            second = await repo.approve_pending(pid)
            assert second is False

    asyncio.run(_run())


# ── Reject CAS ────────────────────────────────────────────────────────────────


def test_reject_moves_to_cancelled(db_session, profile_id):
    """reject_pending: PENDING_APPROVAL → CANCELLED."""
    from crosspost.db.models import ScheduledPostStatus
    from crosspost.db.publication_repo import PublicationRepository

    async def _run():
        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            post = await repo.create_scheduled(
                content_type="post",
                text="to reject",
                title=None,
                media_paths=[],
                channels=["telegram"],
                scheduled_at=datetime.now() + timedelta(hours=1),
                initial_status=ScheduledPostStatus.PENDING_APPROVAL,
            )
            pid = post.id

        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            ok = await repo.reject_pending(pid)
            assert ok is True
            post = await repo.get_scheduled(pid)
            assert post is not None
            assert post.status == ScheduledPostStatus.CANCELLED

    asyncio.run(_run())


def test_approved_post_appears_in_scheduler_queue(db_session, profile_id):
    """After approve, the post must be visible to SchedulerRepository.list_scheduled_active."""
    from crosspost.db.models import ScheduledPostStatus
    from crosspost.db.publication_repo import PublicationRepository
    from crosspost.db.scheduler_repo import SchedulerRepository

    async def _run():
        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            post = await repo.create_scheduled(
                content_type="post",
                text="will be approved",
                title=None,
                media_paths=[],
                channels=["telegram"],
                scheduled_at=datetime.now() - timedelta(minutes=1),
                initial_status=ScheduledPostStatus.PENDING_APPROVAL,
            )
            pid = post.id

        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            await repo.approve_pending(pid)

        async with db_session() as s:
            sched_repo = SchedulerRepository(s)
            active = await sched_repo.list_scheduled_active()
            ids = [p.id for p in active]
            assert pid in ids

    asyncio.run(_run())


# ── list_scheduled includes PENDING_APPROVAL ──────────────────────────────────


def test_list_scheduled_includes_pending(db_session, profile_id):
    """list_scheduled returns both SCHEDULED and PENDING_APPROVAL posts."""
    from crosspost.db.models import ScheduledPostStatus
    from crosspost.db.publication_repo import PublicationRepository

    async def _run():
        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            await repo.create_scheduled(
                content_type="post",
                text="scheduled",
                title=None,
                media_paths=[],
                channels=["telegram"],
                scheduled_at=datetime.now() + timedelta(hours=1),
                initial_status=ScheduledPostStatus.SCHEDULED,
            )
            await repo.create_scheduled(
                content_type="post",
                text="pending",
                title=None,
                media_paths=[],
                channels=["telegram"],
                scheduled_at=datetime.now() + timedelta(hours=2),
                initial_status=ScheduledPostStatus.PENDING_APPROVAL,
            )

        async with db_session() as s:
            repo = PublicationRepository(s, profile_id=profile_id)
            posts = await repo.list_scheduled()
            statuses = {str(p.status) for p in posts}
            assert "scheduled" in statuses
            assert "pending_approval" in statuses

    asyncio.run(_run())
