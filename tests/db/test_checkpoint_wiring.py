"""Wiring and concurrency tests for the async CheckpointStore.

1. A terminal publication (all channels succeed) has its pub#step checkpoints removed.
2. A still-pending publication (partial failure) keeps its checkpoints.
3. Store calls do not block the event loop (a concurrent coroutine keeps running).
4. build_profile_adapter wires the SQLAlchemy CheckpointStore (not InMemory).
5. No stdlib sqlite3 import remains in production code paths.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.sqlite_store import CheckpointStore
from crosspost.orchestrator.publish_service import ChannelOutcome


async def _engine(db):
    return await create_engine_and_tables(f"sqlite+aiosqlite:///{db}")


# ── 1. Terminal publication clears checkpoints ────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_if_final_all_success_removes_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    eng = await _engine(str(tmp_path / "test.db"))
    async with AsyncSession(eng) as session:
        store = CheckpointStore(session, profile_id=1)
        pub_id = "pub_final"
        await store.mark_done(f"{pub_id}#container", "instagram", "cid", target="ig")
        await store.mark_done(f"{pub_id}#photo", "telegram", "msg:1", target="@ch")

        from crosspost.web.routes.publish import _cleanup_if_final

        await _cleanup_if_final(
            session,
            1,
            pub_id,
            [ChannelOutcome("instagram", "done"), ChannelOutcome("telegram", "done")],
        )

        assert await store.get_external_id(f"{pub_id}#container", "instagram", target="ig") is None
        assert await store.get_external_id(f"{pub_id}#photo", "telegram", target="@ch") is None
    await eng.dispose()


# ── 2. Pending publication keeps checkpoints ─────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_if_final_partial_failure_keeps_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    eng = await _engine(str(tmp_path / "test.db"))
    async with AsyncSession(eng) as session:
        store = CheckpointStore(session, profile_id=1)
        pub_id = "pub_partial"
        await store.mark_done(f"{pub_id}#container", "instagram", "cid", target="ig")

        from crosspost.web.routes.publish import _cleanup_if_final

        await _cleanup_if_final(
            session,
            1,
            pub_id,
            [ChannelOutcome("instagram", "done"), ChannelOutcome("telegram", "failed")],
        )

        assert await store.get_external_id(f"{pub_id}#container", "instagram", target="ig") == "cid"
    await eng.dispose()


# ── 3. Store ops don't block the event loop ──────────────────────────────────


@pytest.mark.asyncio
async def test_store_calls_do_not_block_event_loop(tmp_path):
    """A background coroutine keeps ticking while awaited store ops run."""
    eng = await _engine(str(tmp_path / "test.db"))
    ticks = 0

    async def background() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    async with AsyncSession(eng) as session:
        store = CheckpointStore(session, profile_id=1)
        task = asyncio.create_task(background())
        try:
            for i in range(50):
                await store.mark_done(f"pub{i}#c", "instagram", f"cid{i}", target="t")
        finally:
            task.cancel()

        # The loop stayed responsive: the background coroutine ran during the writes.
        assert ticks > 0
        # And all writes landed.
        assert await store.get_external_id("pub49#c", "instagram", target="t") == "cid49"
    await eng.dispose()


# ── 4. Factory wires the SQLAlchemy store ────────────────────────────────────


@pytest.mark.asyncio
async def test_factory_wires_sqlalchemy_store_not_inmemory():
    repo = MagicMock()
    repo.get_credential = AsyncMock(return_value="TELEGRAPH-TOKEN")
    repo.get_connection = AsyncMock(return_value=None)  # no connection record → pass through
    repo.session = MagicMock()  # CheckpointStore only stores the session at init
    repo.vault = MagicMock()

    from crosspost.orchestrator.adapter_factory import build_profile_adapter

    adapter = await build_profile_adapter(repo, profile_id=7, channel="telegraph")

    assert adapter is not None
    assert isinstance(adapter._store, CheckpointStore)


# ── 5. No stdlib sqlite3 import in production code paths ──────────────────────


def test_no_sqlite3_import_in_production():
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "crosspost"
    offenders = []
    pat = re.compile(r"^\s*import sqlite3\b|^\s*from sqlite3\b", re.MULTILINE)
    for p in src.rglob("*.py"):
        if pat.search(p.read_text(encoding="utf-8")):
            offenders.append(str(p.relative_to(src)))
    assert not offenders, f"stdlib sqlite3 imported in production: {offenders}"
