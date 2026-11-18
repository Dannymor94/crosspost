"""Tests for CircuitBreakerRepository (T-203).

All DB operations tested against in-memory SQLite (same approach as test_executor.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import BreakerState
from crosspost.orchestrator.breaker import CircuitBreakerRepository

_T0 = datetime(2024, 1, 1, 12, 0, 0)  # arbitrary naive anchor
_THRESHOLD = 3
_COOLDOWN = 300.0  # seconds


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _repo(factory) -> tuple[CircuitBreakerRepository, AsyncSession]:
    """Return (repo, session) — caller must close the session."""
    session = factory()
    return CircuitBreakerRepository(session), session


# ── Tests: initial state ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_row_returns_closed(factory):
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        state = await repo.get_state(1, "telegram", now=_T0)
    assert state == BreakerState.CLOSED


# ── Tests: failure counting and threshold ─────────────────────────────────────


@pytest.mark.asyncio
async def test_below_threshold_stays_closed(factory):
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        for _ in range(_THRESHOLD - 1):
            await repo.record_failure(
                1, "telegram", now=_T0, threshold=_THRESHOLD, cooldown_seconds=_COOLDOWN
            )
        state = await repo.get_state(1, "telegram", now=_T0)
    assert state == BreakerState.CLOSED


@pytest.mark.asyncio
async def test_threshold_reached_opens_circuit(factory):
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        for _ in range(_THRESHOLD):
            await repo.record_failure(
                1, "telegram", now=_T0, threshold=_THRESHOLD, cooldown_seconds=_COOLDOWN
            )
        state = await repo.get_state(1, "telegram", now=_T0)
    assert state == BreakerState.OPEN


@pytest.mark.asyncio
async def test_open_circuit_blocks_before_cooldown(factory):
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        for _ in range(_THRESHOLD):
            await repo.record_failure(
                1, "telegram", now=_T0, threshold=_THRESHOLD, cooldown_seconds=_COOLDOWN
            )
        # Still within cooldown window
        state = await repo.get_state(1, "telegram", now=_T0 + timedelta(seconds=_COOLDOWN - 1))
    assert state == BreakerState.OPEN


# ── Tests: auto-transition OPEN → HALF_OPEN ───────────────────────────────────


@pytest.mark.asyncio
async def test_open_transitions_to_half_open_after_cooldown(factory):
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        for _ in range(_THRESHOLD):
            await repo.record_failure(
                1, "telegram", now=_T0, threshold=_THRESHOLD, cooldown_seconds=_COOLDOWN
            )
        state = await repo.get_state(1, "telegram", now=_T0 + timedelta(seconds=_COOLDOWN + 1))
    assert state == BreakerState.HALF_OPEN


@pytest.mark.asyncio
async def test_half_open_state_persisted_after_auto_transition(factory):
    """get_state twice after cooldown: second call still sees HALF_OPEN."""
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        for _ in range(_THRESHOLD):
            await repo.record_failure(
                1, "telegram", now=_T0, threshold=_THRESHOLD, cooldown_seconds=_COOLDOWN
            )
        t_after = _T0 + timedelta(seconds=_COOLDOWN + 1)
        await repo.get_state(1, "telegram", now=t_after)
        state = await repo.get_state(1, "telegram", now=t_after)
    assert state == BreakerState.HALF_OPEN


# ── Tests: probe acquisition ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_try_acquire_half_open_succeeds_once(factory):
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        for _ in range(_THRESHOLD):
            await repo.record_failure(
                1, "telegram", now=_T0, threshold=_THRESHOLD, cooldown_seconds=_COOLDOWN
            )
        t_after = _T0 + timedelta(seconds=_COOLDOWN + 1)
        await repo.get_state(1, "telegram", now=t_after)  # trigger auto-transition
        acquired = await repo.try_acquire_half_open(
            1, "telegram", now=t_after, cooldown_seconds=_COOLDOWN
        )
    assert acquired is True


@pytest.mark.asyncio
async def test_try_acquire_half_open_second_call_returns_false(factory):
    """Only one probe can be acquired — second call returns False."""
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        for _ in range(_THRESHOLD):
            await repo.record_failure(
                1, "telegram", now=_T0, threshold=_THRESHOLD, cooldown_seconds=_COOLDOWN
            )
        t_after = _T0 + timedelta(seconds=_COOLDOWN + 1)
        await repo.get_state(1, "telegram", now=t_after)
        await repo.try_acquire_half_open(1, "telegram", now=t_after, cooldown_seconds=_COOLDOWN)
        acquired2 = await repo.try_acquire_half_open(
            1, "telegram", now=t_after, cooldown_seconds=_COOLDOWN
        )
    assert acquired2 is False


@pytest.mark.asyncio
async def test_try_acquire_on_closed_returns_false(factory):
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        acquired = await repo.try_acquire_half_open(
            1, "telegram", now=_T0, cooldown_seconds=_COOLDOWN
        )
    assert acquired is False


# ── Tests: success resets circuit ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_success_after_failures_resets_to_closed(factory):
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        for _ in range(_THRESHOLD - 1):
            await repo.record_failure(
                1, "telegram", now=_T0, threshold=_THRESHOLD, cooldown_seconds=_COOLDOWN
            )
        await repo.record_success(1, "telegram")
        state = await repo.get_state(1, "telegram", now=_T0)
    assert state == BreakerState.CLOSED


@pytest.mark.asyncio
async def test_success_on_no_row_is_noop(factory):
    """record_success when no row exists must not raise or create a row."""
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        await repo.record_success(1, "telegram")  # should not raise
        state = await repo.get_state(1, "telegram", now=_T0)
    assert state == BreakerState.CLOSED


# ── Tests: profile isolation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_different_profiles_isolated(factory):
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        # Trip breaker for profile 1
        for _ in range(_THRESHOLD):
            await repo.record_failure(
                1, "telegram", now=_T0, threshold=_THRESHOLD, cooldown_seconds=_COOLDOWN
            )
        # Profile 2 same channel → still CLOSED
        state2 = await repo.get_state(2, "telegram", now=_T0)
    assert state2 == BreakerState.CLOSED


@pytest.mark.asyncio
async def test_different_channels_isolated(factory):
    async with factory() as s:
        repo = CircuitBreakerRepository(s)
        for _ in range(_THRESHOLD):
            await repo.record_failure(
                1, "telegram", now=_T0, threshold=_THRESHOLD, cooldown_seconds=_COOLDOWN
            )
        state_vk = await repo.get_state(1, "vk_wall", now=_T0)
    assert state_vk == BreakerState.CLOSED
