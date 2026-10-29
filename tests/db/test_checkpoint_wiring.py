"""Tests for checkpoint cleanup wiring and concurrency.

Three scenarios:
1. A publication that reaches terminal state (all channels succeed) has its
   pub#step checkpoints removed by _cleanup_if_final.
2. A publication still pending (partial failure, retry possible) keeps checkpoints.
3. Concurrent writes from the async engine and the sync checkpoint store do not
   raise OperationalError ("database is locked").
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.sqlite_store import SQLiteIdempotencyStore, SyncSQLiteIdempotencyStore
from crosspost.orchestrator.publish_service import ChannelOutcome

# ── 1. Terminal publication clears checkpoints ────────────────────────────────


def test_cleanup_if_final_all_success_removes_checkpoints(tmp_path, monkeypatch):
    """_cleanup_if_final: when all channels succeed, pub#step checkpoints are removed."""
    db = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_URL", f"sqlite+aiosqlite:///{db}")

    store = SyncSQLiteIdempotencyStore(db, profile_id=1)
    pub_id = "pub_final"
    store.mark_done(f"{pub_id}#container", "instagram", "cid", target="ig")
    store.mark_done(f"{pub_id}#photo", "telegram", "msg:1", target="@ch")

    from crosspost.web.routes.publish import _cleanup_if_final

    # All channels succeeded → terminal
    _cleanup_if_final(
        1,
        pub_id,
        [ChannelOutcome("instagram", "done"), ChannelOutcome("telegram", "done")],
    )

    assert store.get_external_id(f"{pub_id}#container", "instagram", target="ig") is None
    assert store.get_external_id(f"{pub_id}#photo", "telegram", target="@ch") is None


# ── 2. Pending publication keeps checkpoints ─────────────────────────────────


def test_cleanup_if_final_partial_failure_keeps_checkpoints(tmp_path, monkeypatch):
    """_cleanup_if_final: when any channel failed, checkpoints are NOT removed."""
    db = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_URL", f"sqlite+aiosqlite:///{db}")

    store = SyncSQLiteIdempotencyStore(db, profile_id=1)
    pub_id = "pub_partial"
    store.mark_done(f"{pub_id}#container", "instagram", "cid", target="ig")

    from crosspost.web.routes.publish import _cleanup_if_final

    # Partial failure → not terminal
    _cleanup_if_final(
        1,
        pub_id,
        [ChannelOutcome("instagram", "done"), ChannelOutcome("telegram", "failed")],
    )

    assert store.get_external_id(f"{pub_id}#container", "instagram", target="ig") == "cid"


# ── 3. Concurrent writes: no database is locked ───────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_async_and_sync_writes_no_locked_error(tmp_path):
    """Async engine writes and sync store writes to the same DB file without locking."""
    db = str(tmp_path / "concurrent.db")
    engine = await create_engine_and_tables(f"sqlite+aiosqlite:///{db}")
    store = SyncSQLiteIdempotencyStore(db, profile_id=1)

    errors: list[Exception] = []

    def sync_writes() -> None:
        try:
            for i in range(30):
                store.mark_done(f"pub{i}#c", "instagram", f"cid{i}", target="t")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        astore = SQLiteIdempotencyStore(session, profile_id=1)
        t = threading.Thread(target=sync_writes)
        t.start()
        for i in range(30):
            await astore.mark_done(f"pub{i}", "instagram", f"media{i}", target="t")
        t.join(timeout=15)

    await engine.dispose()
    assert not errors, f"concurrent writes raised: {errors}"
