"""Persistence tests for SyncSQLiteIdempotencyStore and production factory wiring.

Covers:
1. Instagram #container checkpoint survives process restart (new store instance, same DB).
2. Telegram #photo checkpoint survives process restart.
3. Profile isolation: checkpoints of profile 1 invisible to profile 2.
4. delete_checkpoints removes step-keys but leaves the terminal publication record.
5. Production factory supplies SyncSQLiteIdempotencyStore, not InMemory.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from crosspost.db.sqlite_store import SyncSQLiteIdempotencyStore

# ── helpers ──────────────────────────────────────────────────────────────────


def _store(db_path: str, profile_id: int = 1) -> SyncSQLiteIdempotencyStore:
    return SyncSQLiteIdempotencyStore(str(db_path), profile_id=profile_id)


# ── 1. Instagram container checkpoint survives restart ────────────────────────


def test_instagram_container_checkpoint_survives_restart(tmp_path):
    """New store instance over the same DB file sees the previously written container id."""
    db = str(tmp_path / "store.db")
    pub_id = "abc123"
    container_id = "container_456"

    store1 = _store(db)
    store1.mark_done(f"{pub_id}#container", "instagram", container_id, target="ig_user_1")

    # Simulate restart: brand-new store object, same file.
    store2 = _store(db)
    assert (
        store2.get_external_id(f"{pub_id}#container", "instagram", target="ig_user_1")
        == container_id
    )
    # Final publication not yet done.
    assert not store2.is_done(pub_id, "instagram", target="ig_user_1")


# ── 2. Telegram #photo checkpoint survives restart ────────────────────────────


def test_telegram_photo_checkpoint_survives_restart(tmp_path):
    """Telegram two-message flow: #photo checkpoint persists across restarts."""
    db = str(tmp_path / "store.db")
    pub_id = "tg_pub_789"
    photo_msg_id = "msg:101"

    store1 = _store(db)
    store1.mark_done(f"{pub_id}#photo", "telegram", photo_msg_id, target="@my_channel")

    store2 = _store(db)
    assert (
        store2.get_external_id(f"{pub_id}#photo", "telegram", target="@my_channel") == photo_msg_id
    )
    assert not store2.is_done(pub_id, "telegram", target="@my_channel")


# ── 3. Profile isolation ──────────────────────────────────────────────────────


def test_profile_isolation(tmp_path):
    """Checkpoints from profile 1 are invisible to profile 2 and vice versa."""
    db = str(tmp_path / "store.db")

    s1 = _store(db, profile_id=1)
    s2 = _store(db, profile_id=2)

    s1.mark_done("pub#container", "instagram", "cid_p1", target="ig_user")

    assert s2.get_external_id("pub#container", "instagram", target="ig_user") is None
    assert not s2.is_done("pub#container", "instagram", target="ig_user")

    # Profile 2 can write its own entry under the same key.
    s2.mark_done("pub#container", "instagram", "cid_p2", target="ig_user")
    assert s1.get_external_id("pub#container", "instagram", target="ig_user") == "cid_p1"
    assert s2.get_external_id("pub#container", "instagram", target="ig_user") == "cid_p2"


# ── 4. delete_checkpoints removes step-keys only ─────────────────────────────


def test_delete_checkpoints_leaves_terminal_record(tmp_path):
    """Checkpoint step-keys (pub#step) are removed; the final pub record survives."""
    db = str(tmp_path / "store.db")
    store = _store(db)
    pub_id = "pub_xyz"

    store.mark_done(f"{pub_id}#container", "instagram", "cid", target="ig_user")
    store.mark_done(pub_id, "instagram", "media_123", target="ig_user")

    # Both exist before cleanup.
    assert store.get_external_id(f"{pub_id}#container", "instagram", target="ig_user") == "cid"
    assert store.is_done(pub_id, "instagram", target="ig_user")

    store.delete_checkpoints(pub_id)

    # Checkpoint gone, terminal record intact.
    assert store.get_external_id(f"{pub_id}#container", "instagram", target="ig_user") is None
    assert store.is_done(pub_id, "instagram", target="ig_user")
    assert store.get_external_id(pub_id, "instagram", target="ig_user") == "media_123"


# ── 5. Production factory wires persistent store ─────────────────────────────


@pytest.mark.asyncio
async def test_factory_wires_persistent_store_not_inmemory(tmp_path, monkeypatch):
    """build_profile_adapter with store=None must create SyncSQLiteIdempotencyStore."""
    # Point DB_URL to a temp file so the store can initialise its schema.
    monkeypatch.setenv("DB_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")

    # Mock ProfileRepository: get_credential returns a valid Telegraph token.
    repo = MagicMock()
    repo.get_credential = AsyncMock(return_value="TELEGRAPH-TOKEN")
    repo.session = MagicMock()
    repo.vault = MagicMock()

    from crosspost.orchestrator.adapter_factory import build_profile_adapter

    adapter = await build_profile_adapter(repo, profile_id=7, channel="telegraph")

    assert adapter is not None
    assert isinstance(adapter._store, SyncSQLiteIdempotencyStore)
