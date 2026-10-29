"""Persistence tests for the async CheckpointStore (SQLAlchemy engine).

Covers:
1. Instagram #container checkpoint survives a simulated process restart.
2. Telegram #photo checkpoint survives a simulated restart.
3. Profile isolation: checkpoints of profile 1 invisible to profile 2.
4. delete_checkpoints removes step-keys but leaves the terminal publication record.
5. delete_checkpoints("schedpost-5") does not touch "schedpost-50" (prefix-match pin).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.sqlite_store import CheckpointStore


async def _engine(db):
    return await create_engine_and_tables(f"sqlite+aiosqlite:///{db}")


# ── 1. Instagram container checkpoint survives restart ────────────────────────


@pytest.mark.asyncio
async def test_instagram_container_checkpoint_survives_restart(tmp_path):
    """A brand-new engine/session over the same DB file sees the written container id."""
    db = str(tmp_path / "store.db")
    pub_id, container_id = "abc123", "container_456"

    eng1 = await _engine(db)
    async with AsyncSession(eng1) as s1:
        await CheckpointStore(s1, profile_id=1).mark_done(
            f"{pub_id}#container", "instagram", container_id, target="ig_user_1"
        )
    await eng1.dispose()

    # Simulate restart: new engine + session, same file.
    eng2 = await _engine(db)
    async with AsyncSession(eng2) as s2:
        store = CheckpointStore(s2, profile_id=1)
        assert (
            await store.get_external_id(f"{pub_id}#container", "instagram", target="ig_user_1")
            == container_id
        )
        assert not await store.is_done(pub_id, "instagram", target="ig_user_1")
    await eng2.dispose()


# ── 2. Telegram #photo checkpoint survives restart ────────────────────────────


@pytest.mark.asyncio
async def test_telegram_photo_checkpoint_survives_restart(tmp_path):
    db = str(tmp_path / "store.db")
    pub_id, photo_msg_id = "tg_pub_789", "msg:101"

    eng1 = await _engine(db)
    async with AsyncSession(eng1) as s1:
        await CheckpointStore(s1, profile_id=1).mark_done(
            f"{pub_id}#photo", "telegram", photo_msg_id, target="@my_channel"
        )
    await eng1.dispose()

    eng2 = await _engine(db)
    async with AsyncSession(eng2) as s2:
        store = CheckpointStore(s2, profile_id=1)
        assert (
            await store.get_external_id(f"{pub_id}#photo", "telegram", target="@my_channel")
            == photo_msg_id
        )
        assert not await store.is_done(pub_id, "telegram", target="@my_channel")
    await eng2.dispose()


# ── 3. Profile isolation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_profile_isolation(tmp_path):
    """Checkpoints from profile 1 are invisible to profile 2 and vice versa."""
    db = str(tmp_path / "store.db")
    eng = await _engine(db)
    async with AsyncSession(eng) as session:
        s1 = CheckpointStore(session, profile_id=1)
        s2 = CheckpointStore(session, profile_id=2)

        await s1.mark_done("pub#container", "instagram", "cid_p1", target="ig_user")

        assert await s2.get_external_id("pub#container", "instagram", target="ig_user") is None
        assert not await s2.is_done("pub#container", "instagram", target="ig_user")

        # Profile 2 writes its own entry under the same key — stays separate.
        await s2.mark_done("pub#container", "instagram", "cid_p2", target="ig_user")
        assert await s1.get_external_id("pub#container", "instagram", target="ig_user") == "cid_p1"
        assert await s2.get_external_id("pub#container", "instagram", target="ig_user") == "cid_p2"
    await eng.dispose()


# ── 4. delete_checkpoints removes step-keys only ─────────────────────────────


@pytest.mark.asyncio
async def test_delete_checkpoints_leaves_terminal_record(tmp_path):
    db = str(tmp_path / "store.db")
    eng = await _engine(db)
    async with AsyncSession(eng) as session:
        store = CheckpointStore(session, profile_id=1)
        pub_id = "pub_xyz"

        await store.mark_done(f"{pub_id}#container", "instagram", "cid", target="ig_user")
        await store.mark_done(pub_id, "instagram", "media_123", target="ig_user")

        assert (
            await store.get_external_id(f"{pub_id}#container", "instagram", target="ig_user")
            == "cid"
        )
        assert await store.is_done(pub_id, "instagram", target="ig_user")

        await store.delete_checkpoints(pub_id)

        assert (
            await store.get_external_id(f"{pub_id}#container", "instagram", target="ig_user")
            is None
        )
        assert await store.is_done(pub_id, "instagram", target="ig_user")
        assert await store.get_external_id(pub_id, "instagram", target="ig_user") == "media_123"
    await eng.dispose()


# ── 5. Prefix-match safety: schedpost-5 must not match schedpost-50 ───────────


@pytest.mark.asyncio
async def test_delete_checkpoints_prefix_does_not_match_sibling(tmp_path):
    db = str(tmp_path / "store.db")
    eng = await _engine(db)
    async with AsyncSession(eng) as session:
        store = CheckpointStore(session, profile_id=1)
        await store.mark_done("schedpost-5#container", "instagram", "cid5", target="t")
        await store.mark_done("schedpost-50#container", "instagram", "cid50", target="t")

        await store.delete_checkpoints("schedpost-5")

        assert await store.get_external_id("schedpost-5#container", "instagram", target="t") is None
        # Sibling with a longer id must survive.
        assert (
            await store.get_external_id("schedpost-50#container", "instagram", target="t")
            == "cid50"
        )
    await eng.dispose()
