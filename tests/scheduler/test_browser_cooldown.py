"""Tests for browser-tier per-account session cooldown.

Verifies:
1. Second browser publication within cooldown window → deferred, attempt counter unchanged.
2. After cooldown elapses → publishes normally.
3. Different accounts (profile_ids) do not gate each other.
4. API-tier channel (telegram) is never gated, even during browser cooldown.
5. Cooldown state survives an executor restart (persisted in DB).
6. Fan-out to two targets on one account: second target deferred within same tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.adapters.base import ChannelResult, ResultStatus
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.log_repo import LogRepository
from crosspost.db.models import ConnectionState, CredentialKind
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.scheduler_repo import SchedulerRepository
from crosspost.db.vault import get_vault
from crosspost.scheduler.browser_cooldown import BROWSER_CHANNELS, BrowserCooldown
from crosspost.scheduler.executor import ScheduledExecutor, scheduled_publication_id

_PATCH = "crosspost.scheduler.executor.build_profile_adapter"

_COOLDOWN = 300.0  # seconds — used in all tests
_PAST = datetime(2026, 1, 1, 12, 0, 0)  # t=0
_SOON = _PAST + timedelta(seconds=_COOLDOWN - 1)  # still cooling
_AFTER = _PAST + timedelta(seconds=_COOLDOWN + 1)  # cooldown elapsed


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────


class _MockAdapter:
    def __init__(self, channel, *, result=None):
        self.channel = channel
        self._result = result
        self.calls = 0

    async def publish(self, content, *, publication_id):
        self.calls += 1
        return self._result or ChannelResult(self.channel, ResultStatus.DONE, external_id="ok")


def _patch_adapters(mapping):
    async def fake_build(repo, profile_id, channel, target_id="", *, store=None):
        return mapping.get(channel)

    return patch(_PATCH, new=fake_build)


async def _make_profile(factory, channels, *, name="p") -> int:
    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile(name)
        for ch in channels:
            await repo.upsert_connection(p.id, ch, ConnectionState.LIVE)
            # vk_wall / vk_channel / yandex need a storage_state credential to build an adapter
            if ch in BROWSER_CHANNELS:
                await repo.set_credential(p.id, ch, CredentialKind.STORAGE_STATE, "{}")
        return p.id


async def _seed_post(factory, profile_id, when, channels):
    import os

    media_dir = Path(os.environ["MEDIA_DIR"]) / "sched-x"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "00.jpg").write_bytes(b"\xff\xd8\xff")

    async with factory() as s:
        repo = PublicationRepository(s, profile_id=profile_id)
        post = await repo.create_scheduled(
            content_type="post",
            text="hi",
            title=None,
            media_paths=[str(media_dir / "00.jpg")],
            channels=channels,
            scheduled_at=when,
        )
        return post.id


async def _post_status(factory, post_id) -> str | None:
    async with factory() as s:
        post = await SchedulerRepository(s).get(post_id)
        return None if post is None else str(post.status)


async def _pub_rows(factory, profile_id, post_id) -> dict:
    async with factory() as s:
        repo = PublicationRepository(s, profile_id=profile_id)
        rows = await repo.list_statuses(scheduled_publication_id(post_id))
        return {(r.channel, r.target): r for r in rows}


async def _cooldown_log(factory, profile_id, post_id) -> list[str]:
    """Return log messages for this post that mention 'deferred'."""
    async with factory() as s:
        repo = LogRepository(s)
        entries = await repo.list_for_profile(profile_id)
    pubid = scheduled_publication_id(post_id)
    return [e.message for e in entries if e.publication_id == pubid and "deferred" in e.message]


def _executor(factory, *, cooldown=_COOLDOWN) -> ScheduledExecutor:
    return ScheduledExecutor(
        factory,
        pacing_seconds=0,
        poll_seconds=0,
        browser_session_cooldown=cooldown,
    )


# ── Test 1: second publication within window is deferred, attempt unchanged ───


async def test_cooldown_defers_second_browser_publication(factory):
    """Second browser-tier dispatch within cooldown window → PENDING, no attempt bump."""
    pid = await _make_profile(factory, ["vk_wall"], name="cool1")
    post_id = await _seed_post(factory, pid, _PAST, ["vk_wall"])
    adapter = _MockAdapter("vk_wall")
    ex = _executor(factory)

    with _patch_adapters({"vk_wall": adapter}):
        # First tick: publishes and records session timestamp.
        await ex.run_once(now=_PAST)

    assert await _post_status(factory, post_id) in ("done", "failed", "scheduled")

    # Seed a second post (same account) immediately within the cooldown window.
    post_id2 = await _seed_post(factory, pid, _PAST, ["vk_wall"])
    with _patch_adapters({"vk_wall": adapter}):
        await ex.run_once(now=_SOON)  # still within cooldown

    # Post 2 should still be SCHEDULED (deferred back).
    assert await _post_status(factory, post_id2) == "scheduled"

    # Publication row should not exist yet (no attempt made)...
    rows = await _pub_rows(factory, pid, post_id2)
    row = rows.get(("vk_wall", "")) or rows.get(("vk_wall", None))
    if row is not None:
        assert row.attempt_count == 0, "cooldown deferral must not consume a retry attempt"

    # Deferral must appear in logs as INFO, not ERROR.
    deferred_msgs = await _cooldown_log(factory, pid, post_id2)
    assert deferred_msgs, "expected a 'deferred' log entry for the cooled-down channel"


# ── Test 2: after window elapses → publishes normally ─────────────────────────


async def test_publishes_after_cooldown_elapses(factory):
    """After the cooldown window passes, the post dispatches normally."""
    pid = await _make_profile(factory, ["vk_wall"], name="cool2")
    adapter = _MockAdapter("vk_wall")
    ex = _executor(factory)

    # First post at t=0.
    await _seed_post(factory, pid, _PAST, ["vk_wall"])
    with _patch_adapters({"vk_wall": adapter}):
        await ex.run_once(now=_PAST)

    # Second post after cooldown elapses.
    post2 = await _seed_post(factory, pid, _AFTER, ["vk_wall"])
    with _patch_adapters({"vk_wall": adapter}):
        await ex.run_once(now=_AFTER)

    assert await _post_status(factory, post2) in ("done", "failed"), (
        "post should have been dispatched after cooldown elapsed"
    )
    rows = await _pub_rows(factory, pid, post2)
    row = rows.get(("vk_wall", ""))
    assert row is not None and row.attempt_count >= 1


# ── Test 3: different accounts do not gate each other ─────────────────────────


async def test_different_accounts_do_not_gate_each_other(factory):
    """Cooldown on profile A does not affect profile B."""
    pid_a = await _make_profile(factory, ["vk_wall"], name="acct_a")
    pid_b = await _make_profile(factory, ["vk_wall"], name="acct_b")
    adapter = _MockAdapter("vk_wall")
    ex = _executor(factory)

    # Publish from A at t=0 to establish its cooldown.
    await _seed_post(factory, pid_a, _PAST, ["vk_wall"])
    with _patch_adapters({"vk_wall": adapter}):
        await ex.run_once(now=_PAST)

    # Publish from B within A's cooldown window — B should NOT be gated.
    post_b = await _seed_post(factory, pid_b, _PAST, ["vk_wall"])
    with _patch_adapters({"vk_wall": adapter}):
        await ex.run_once(now=_SOON)

    assert await _post_status(factory, post_b) in ("done", "failed"), (
        "profile B must not be gated by profile A's cooldown"
    )


# ── Test 4: API-tier channel is never gated ───────────────────────────────────


async def test_api_tier_channel_never_gated(factory):
    """Telegram (API-tier) is dispatched even when browser cooldown is active."""
    pid = await _make_profile(factory, ["vk_wall", "telegram"], name="apitier")

    # Manually record a recent vk_wall session so the account looks cooling.
    async with factory() as s:
        cd = BrowserCooldown(s, cooldown_seconds=_COOLDOWN)
        await cd.record_session(pid, "vk_wall", _PAST)

    tg_adapter = _MockAdapter("telegram")
    vk_adapter = _MockAdapter("vk_wall")

    # Post to telegram only — should dispatch without any gating.
    post_id = await _seed_post(factory, pid, _PAST, ["telegram"])
    ex = _executor(factory)
    with _patch_adapters({"telegram": tg_adapter, "vk_wall": vk_adapter}):
        await ex.run_once(now=_SOON)  # within vk_wall cooldown

    assert await _post_status(factory, post_id) in ("done", "failed"), (
        "telegram must not be gated by vk_wall cooldown"
    )
    assert tg_adapter.calls == 1
    assert vk_adapter.calls == 0


# ── Test 5: cooldown survives executor restart ────────────────────────────────


async def test_cooldown_survives_restart(factory):
    """Cooldown timestamp is persisted; a new executor instance honours it."""
    pid = await _make_profile(factory, ["vk_wall"], name="restart")

    # Executor 1: publish post 1 at t=0 → records session timestamp.
    await _seed_post(factory, pid, _PAST, ["vk_wall"])
    ex1 = _executor(factory)
    with _patch_adapters({"vk_wall": _MockAdapter("vk_wall")}):
        await ex1.run_once(now=_PAST)

    # Executor 2: fresh instance, simulates restart.
    post2 = await _seed_post(factory, pid, _PAST, ["vk_wall"])
    ex2 = _executor(factory)  # brand-new instance, no in-memory state
    adapter2 = _MockAdapter("vk_wall")
    with _patch_adapters({"vk_wall": adapter2}):
        await ex2.run_once(now=_SOON)  # still within cooldown

    # Post 2 deferred — the persisted timestamp was respected.
    assert await _post_status(factory, post2) == "scheduled", (
        "new executor instance must read persisted cooldown and defer"
    )
    assert adapter2.calls == 0


# ── Test 6: fan-out — two targets same channel, second deferred ───────────────


async def test_fanout_second_target_deferred_within_tick(factory):
    """Two targets on same browser-tier account: second deferred after first dispatches.

    Design choice: cooldown key is (profile_id, channel), not (profile_id, channel, target).
    A session is opened per-account regardless of target count. The second target on the
    same account/channel therefore hits the cooldown and is deferred to the next tick.
    """
    pid = await _make_profile(factory, ["vk_wall"], name="fanout")

    # Two targets on the same channel — stored as dict items.
    channels = [
        {"channel": "vk_wall", "target_id": "target-1"},
        {"channel": "vk_wall", "target_id": "target-2"},
    ]
    post_id = await _seed_post(factory, pid, _PAST, channels)

    adapter = _MockAdapter("vk_wall")
    ex = _executor(factory)
    with _patch_adapters({"vk_wall": adapter}):
        await ex.run_once(now=_PAST)

    # Post is back to SCHEDULED (one target deferred).
    assert await _post_status(factory, post_id) == "scheduled", (
        "post with fan-out must be re-queued when second target is deferred"
    )

    # Exactly one target was dispatched (the adapter called once).
    assert adapter.calls == 1, "only the first fan-out target should be dispatched per tick"

    # Second target has attempt_count=0 (deferred, no bump).
    rows = await _pub_rows(factory, pid, post_id)
    t2_row = rows.get(("vk_wall", "target-2"))
    if t2_row is not None:
        assert t2_row.attempt_count == 0
