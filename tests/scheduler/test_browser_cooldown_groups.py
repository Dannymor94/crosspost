"""Tests for account-group-based browser cooldown.

vk_wall, vk_channel and vk_clips share one VK account per profile.
Cooldown key must be (profile_id, account_group), not (profile_id, channel),
so that a fan-out to wall + channel triggers only one browser session, not two.

Tests:
1. Fan-out to vk_wall + vk_channel → second channel deferred (same account group).
2. Yandex not gated by VK cooldown on same profile (different account group).
3. Two profiles each with their own VK account do not gate each other.
4. Regression: API tier never gated; cooldown survives restart; attempt counter untouched.
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
from crosspost.scheduler.browser_cooldown import BROWSER_CHANNELS, BrowserCooldown, account_group
from crosspost.scheduler.executor import ScheduledExecutor, scheduled_publication_id

_PATCH = "crosspost.scheduler.executor.build_profile_adapter"
_COOLDOWN = 300.0
_T0 = datetime(2026, 1, 1, 12, 0, 0)
_WITHIN = _T0 + timedelta(seconds=_COOLDOWN - 1)
_AFTER = _T0 + timedelta(seconds=_COOLDOWN + 1)


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


async def _pub_rows(factory, profile_id, post_id):
    async with factory() as s:
        repo = PublicationRepository(s, profile_id=profile_id)
        rows = await repo.list_statuses(scheduled_publication_id(post_id))
        return {(r.channel, r.target): r for r in rows}


async def _deferred_logs(factory, profile_id, post_id) -> list[str]:
    async with factory() as s:
        repo = LogRepository(s)
        entries = await repo.list_for_profile(profile_id)
    pubid = scheduled_publication_id(post_id)
    return [e.message for e in entries if e.publication_id == pubid and "deferred" in e.message]


def _executor(factory, *, cooldown=_COOLDOWN) -> ScheduledExecutor:
    return ScheduledExecutor(
        factory, pacing_seconds=0, poll_seconds=0, browser_session_cooldown=cooldown
    )


# ── Unit: account_group() function ────────────────────────────────────────────


def test_vk_channels_share_account_group():
    """vk_wall and vk_channel resolve to the same account group.
    vk_clips is also checked if its validator is declared (not yet, pending recon pass 2).
    """
    from crosspost.channels.validators import VALIDATORS

    grp = account_group("vk_wall")
    assert grp == account_group("vk_channel")
    if "vk_clips" in VALIDATORS:
        assert grp == account_group("vk_clips"), "vk_clips must share the VK account group"


def test_yandex_has_own_account_group():
    """Yandex has a different account group from VK."""
    assert account_group("yandex") != account_group("vk_wall")


def test_api_channels_have_no_browser_group():
    """API-tier channels are not in BROWSER_CHANNELS; their group is irrelevant to cooldown."""
    assert "telegram" not in BROWSER_CHANNELS
    assert "telegraph" not in BROWSER_CHANNELS


# ── Test 1: fan-out vk_wall + vk_channel → second deferred ───────────────────


async def test_fanout_wall_and_channel_defers_second(factory):
    """Fan-out to vk_wall and vk_channel: second channel deferred (same VK account)."""
    pid = await _make_profile(factory, ["vk_wall", "vk_channel"], name="vk_fan")

    post_id = await _seed_post(factory, pid, _T0, ["vk_wall", "vk_channel"])
    wall_adapter = _MockAdapter("vk_wall")
    chan_adapter = _MockAdapter("vk_channel")
    ex = _executor(factory)

    with _patch_adapters({"vk_wall": wall_adapter, "vk_channel": chan_adapter}):
        await ex.run_once(now=_T0)

    # One was dispatched, the other deferred → post back to SCHEDULED.
    assert await _post_status(factory, post_id) == "scheduled", (
        "post must be re-queued when second VK channel is deferred by shared account cooldown"
    )
    total_calls = wall_adapter.calls + chan_adapter.calls
    assert total_calls == 1, "exactly one VK adapter call expected per tick"

    # Deferred channel: either no row exists, or attempt_count==0.
    rows = await _pub_rows(factory, pid, post_id)
    # One channel was dispatched (attempt_count>=1), the other deferred (no row or count=0).
    dispatched_count = sum(1 for r in rows.values() if r.attempt_count >= 1)
    assert dispatched_count == 1, "exactly one channel should have been dispatched"

    # Log must mention deferral (INFO, not ERROR).
    msgs = await _deferred_logs(factory, pid, post_id)
    assert msgs, "expected a deferred INFO log entry for the second VK channel"


# ── Test 2: Yandex not gated by VK cooldown ───────────────────────────────────


async def test_yandex_not_gated_by_vk_cooldown(factory):
    """Yandex Business is dispatched even when VK is in cooldown on the same profile."""
    pid = await _make_profile(factory, ["vk_wall", "yandex"], name="mixed")

    # Plant a VK session timestamp so VK is cooling.
    async with factory() as s:
        cd = BrowserCooldown(s, cooldown_seconds=_COOLDOWN)
        await cd.record_session(pid, "vk_wall", _T0)

    # Post to Yandex only.
    post_id = await _seed_post(factory, pid, _T0, ["yandex"])
    yz_adapter = _MockAdapter("yandex")
    ex = _executor(factory)
    with _patch_adapters({"yandex": yz_adapter}):
        await ex.run_once(now=_WITHIN)  # within VK cooldown window

    assert await _post_status(factory, post_id) in ("done", "failed"), (
        "Yandex must not be gated by VK cooldown"
    )
    assert yz_adapter.calls == 1


# ── Test 3: two profiles don't gate each other ────────────────────────────────


async def test_two_profiles_independent_cooldown(factory):
    """Profile A's VK cooldown does not affect profile B's VK."""
    pid_a = await _make_profile(factory, ["vk_wall"], name="a")
    pid_b = await _make_profile(factory, ["vk_wall"], name="b")
    ex = _executor(factory)

    await _seed_post(factory, pid_a, _T0, ["vk_wall"])
    with _patch_adapters({"vk_wall": _MockAdapter("vk_wall")}):
        await ex.run_once(now=_T0)  # A dispatches, records cooldown

    post_b = await _seed_post(factory, pid_b, _T0, ["vk_wall"])
    b_adapter = _MockAdapter("vk_wall")
    with _patch_adapters({"vk_wall": b_adapter}):
        await ex.run_once(now=_WITHIN)  # B must not be gated by A's cooldown

    assert await _post_status(factory, post_b) in ("done", "failed"), (
        "profile B's VK must not be gated by profile A's cooldown"
    )
    assert b_adapter.calls == 1


# ── Test 4: regression — API never gated, restart, attempt counter ─────────────


async def test_api_tier_never_gated_by_group_cooldown(factory):
    """Telegram is dispatched even when VK account group is cooling."""
    pid = await _make_profile(factory, ["vk_wall", "telegram"], name="apireg")

    async with factory() as s:
        cd = BrowserCooldown(s, cooldown_seconds=_COOLDOWN)
        await cd.record_session(pid, "vk_wall", _T0)

    post_id = await _seed_post(factory, pid, _T0, ["telegram"])
    tg_adapter = _MockAdapter("telegram")
    ex = _executor(factory)
    with _patch_adapters({"telegram": tg_adapter}):
        await ex.run_once(now=_WITHIN)

    assert await _post_status(factory, post_id) in ("done", "failed")
    assert tg_adapter.calls == 1


async def test_group_cooldown_survives_restart(factory):
    """VK group cooldown persists across executor instances."""
    pid = await _make_profile(factory, ["vk_wall"], name="restart2")

    await _seed_post(factory, pid, _T0, ["vk_wall"])
    ex1 = _executor(factory)
    with _patch_adapters({"vk_wall": _MockAdapter("vk_wall")}):
        await ex1.run_once(now=_T0)

    post2 = await _seed_post(factory, pid, _T0, ["vk_wall"])
    ex2 = _executor(factory)  # fresh instance
    a2 = _MockAdapter("vk_wall")
    with _patch_adapters({"vk_wall": a2}):
        await ex2.run_once(now=_WITHIN)

    assert await _post_status(factory, post2) == "scheduled"
    assert a2.calls == 0


async def test_group_cooldown_attempt_counter_untouched(factory):
    """Deferral due to group cooldown must not consume a retry attempt."""
    pid = await _make_profile(factory, ["vk_wall"], name="cnt")

    async with factory() as s:
        cd = BrowserCooldown(s, cooldown_seconds=_COOLDOWN)
        await cd.record_session(pid, "vk_wall", _T0)

    post_id = await _seed_post(factory, pid, _T0, ["vk_wall"])
    ex = _executor(factory)
    with _patch_adapters({"vk_wall": _MockAdapter("vk_wall")}):
        await ex.run_once(now=_WITHIN)

    rows = await _pub_rows(factory, pid, post_id)
    if rows:
        row = next(iter(rows.values()))
        assert row.attempt_count == 0, "deferred by cooldown must not bump attempt_count"
