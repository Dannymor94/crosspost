"""Тесты HealthChecker и fail-closed dispatch (T-202).

Все тесты на моках: без реального браузера, сети или API.
VALIDATORS подменяется через monkeypatch.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.channels.validators import VALIDATORS, ChannelValidatorDef, ValidatorResult
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ConnectionState, CredentialKind
from crosspost.db.profile_repo import ProfileRepository
from crosspost.orchestrator.adapter_factory import build_profile_adapter
from crosspost.sessions.health import HealthChecker

# ── Фикстуры ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())


@pytest_asyncio.fixture
async def engine():
    e = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    yield e
    await e.dispose()


@pytest_asyncio.fixture
async def session(engine):
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def vault() -> Fernet:
    from crosspost.db.vault import get_vault

    return get_vault()  # same key as HealthChecker.run_once() uses via get_vault()


@pytest_asyncio.fixture
async def repo(session, vault) -> ProfileRepository:
    return ProfileRepository(session, vault=vault)


@pytest_asyncio.fixture
async def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


def _make_def(alive: bool) -> ChannelValidatorDef:
    async def _fn(credential: str | None) -> bool:
        return alive

    return ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_fn)


def _make_raising_def(exc: Exception) -> ChannelValidatorDef:
    async def _fn(credential: str | None) -> bool:
        raise exc

    return ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_fn)


def _make_result_def(result: ValidatorResult) -> ChannelValidatorDef:
    async def _fn(credential: str | None) -> ValidatorResult:
        return result

    return ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_fn)


# ── 1. Истёкшая сессия → needs_relogin ────────────────────────────────────────


async def test_dead_session_sets_needs_relogin(repo, factory, monkeypatch):
    monkeypatch.setitem(VALIDATORS, "telegram", _make_def(alive=False))

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "telegram")
    assert conn is not None
    assert conn.state == ConnectionState.NEEDS_RELOGIN


async def test_dead_session_via_validator_result(repo, factory, monkeypatch):
    monkeypatch.setitem(
        VALIDATORS, "telegram", _make_result_def(ValidatorResult(alive=False, error="expired"))
    )

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "telegram")
    assert conn is not None
    assert conn.state == ConnectionState.NEEDS_RELOGIN


# ── 2. Временный сбой → состояние сохраняется ─────────────────────────────────


async def test_transient_error_preserves_live_state(repo, factory, monkeypatch):
    monkeypatch.setitem(VALIDATORS, "telegram", _make_raising_def(OSError("network unreachable")))

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "telegram")
    assert conn is not None
    assert conn.state == ConnectionState.LIVE  # preserved — not set to needs_relogin


async def test_transient_error_preserves_needs_relogin_state(repo, factory, monkeypatch):
    # Already needs_relogin channels are skipped — still skipped on error
    monkeypatch.setitem(VALIDATORS, "telegram", _make_raising_def(OSError("network unreachable")))

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.NEEDS_RELOGIN)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "telegram")
    assert conn.state == ConnectionState.NEEDS_RELOGIN


# ── 3. Забаненное подключение → не трогается ──────────────────────────────────


async def test_banned_connection_never_overwritten(repo, factory, monkeypatch):
    # Validator says alive=False → but BANNED must stay BANNED
    monkeypatch.setitem(VALIDATORS, "telegram", _make_def(alive=False))

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.BANNED)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "telegram")
    assert conn is not None
    assert conn.state == ConnectionState.BANNED


async def test_banned_connection_not_checked_at_all(repo, factory, monkeypatch):
    called = []

    async def _fn(credential):
        called.append(True)
        return True

    monkeypatch.setitem(
        VALIDATORS,
        "telegram",
        ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_fn),
    )

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.BANNED)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    assert called == []  # validator was never called


# ── 4. Fail-closed: needs_relogin/banned → адаптер не строится ────────────────


async def test_needs_relogin_connection_blocks_adapter(repo, monkeypatch, vault):
    from crosspost.db.sqlite_store import CheckpointStore

    monkeypatch.setenv("VAULT_KEY", vault.key.decode() if hasattr(vault, "key") else "")

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegraph", CredentialKind.API_TOKEN, "tok123")
    await repo.upsert_connection(p.id, "telegraph", ConnectionState.NEEDS_RELOGIN)

    store = CheckpointStore(repo.session, profile_id=p.id)
    adapter = await build_profile_adapter(repo, p.id, "telegraph", store=store)

    assert adapter is None


async def test_banned_connection_blocks_adapter(repo, vault):
    from crosspost.db.sqlite_store import CheckpointStore

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegraph", CredentialKind.API_TOKEN, "tok123")
    await repo.upsert_connection(p.id, "telegraph", ConnectionState.BANNED)

    store = CheckpointStore(repo.session, profile_id=p.id)
    adapter = await build_profile_adapter(repo, p.id, "telegraph", store=store)

    assert adapter is None


# ── 5. Здоровое подключение → адаптер строится ───────────────────────────────


async def test_live_connection_allows_adapter(repo):
    from crosspost.adapters.api.telegraph import TelegraphAdapter
    from crosspost.db.sqlite_store import CheckpointStore

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegraph", CredentialKind.API_TOKEN, "tok123")
    await repo.upsert_connection(p.id, "telegraph", ConnectionState.LIVE)

    store = CheckpointStore(repo.session, profile_id=p.id)
    adapter = await build_profile_adapter(repo, p.id, "telegraph", store=store)

    assert isinstance(adapter, TelegraphAdapter)


async def test_no_connection_record_allows_adapter(repo):
    """Нет записи connection → pass through (обратная совместимость)."""
    from crosspost.adapters.api.telegraph import TelegraphAdapter
    from crosspost.db.sqlite_store import CheckpointStore

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegraph", CredentialKind.API_TOKEN, "tok123")
    # Не создаём connection record

    store = CheckpointStore(repo.session, profile_id=p.id)
    adapter = await build_profile_adapter(repo, p.id, "telegraph", store=store)

    assert isinstance(adapter, TelegraphAdapter)


# ── 6. Несколько профилей → нет межпрофильных изменений ──────────────────────


async def test_multiple_profiles_isolated(repo, factory, monkeypatch):
    # alice: LIVE → dead → needs_relogin
    # bob: LIVE → healthy → stays LIVE
    calls = {"alice": False, "bob": False}

    async def _alice_fn(cred):
        calls["alice"] = True
        return False

    async def _bob_fn(cred):
        calls["bob"] = True
        return True

    # We need two different channels to test isolation
    monkeypatch.setitem(
        VALIDATORS,
        "telegram",
        ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_alice_fn),
    )
    monkeypatch.setitem(
        VALIDATORS,
        "telegraph",
        ChannelValidatorDef(
            kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_bob_fn, enabled=True
        ),
    )

    alice = await repo.create_profile("alice")
    bob = await repo.create_profile("bob")

    await repo.upsert_connection(alice.id, "telegram", ConnectionState.LIVE)
    await repo.upsert_connection(bob.id, "telegraph", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    alice_conn = await repo.get_connection(alice.id, "telegram")
    bob_conn = await repo.get_connection(bob.id, "telegraph")
    # alice's telegram is NOT touched when checking bob's telegraph and vice versa
    assert alice_conn.state == ConnectionState.NEEDS_RELOGIN
    assert bob_conn.state == ConnectionState.LIVE

    # Verify no cross-contamination: alice's telegraph untouched, bob's telegram untouched
    assert await repo.get_connection(alice.id, "telegraph") is None
    assert await repo.get_connection(bob.id, "telegram") is None


# ── 7. Общая сессия VK ────────────────────────────────────────────────────────


async def test_vk_channel_uses_vk_wall_session(repo, factory, monkeypatch, vault):
    """vk_channel.session_channel='vk_wall' → health check reads vk_wall credential."""
    received_credentials: list[str | None] = []

    async def _capture_fn(credential: str | None) -> bool:
        received_credentials.append(credential)
        return True

    monkeypatch.setitem(
        VALIDATORS,
        "vk_channel",
        ChannelValidatorDef(
            kind="browser",
            credential_kind=CredentialKind.STORAGE_STATE,
            fn=_capture_fn,
            session_channel="vk_wall",  # mirrors real declaration
        ),
    )

    p = await repo.create_profile("alice")
    # Store credential under vk_wall (not vk_channel)
    await repo.set_credential(p.id, "vk_wall", CredentialKind.STORAGE_STATE, "vk_session_data")
    await repo.upsert_connection(p.id, "vk_channel", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    # Validator was called with vk_wall credential
    assert received_credentials == ["vk_session_data"]


async def test_vk_both_channels_checked_independently(repo, factory, monkeypatch):
    """vk_wall и vk_channel проверяются отдельно — каждый пишет в свой connection."""
    wall_calls = [0]
    channel_calls = [0]

    async def _wall_fn(cred):
        wall_calls[0] += 1
        return False  # dead

    async def _channel_fn(cred):
        channel_calls[0] += 1
        return False  # dead

    monkeypatch.setitem(
        VALIDATORS,
        "vk_wall",
        ChannelValidatorDef(
            kind="browser", credential_kind=CredentialKind.STORAGE_STATE, fn=_wall_fn
        ),
    )
    monkeypatch.setitem(
        VALIDATORS,
        "vk_channel",
        ChannelValidatorDef(
            kind="browser",
            credential_kind=CredentialKind.STORAGE_STATE,
            fn=_channel_fn,
            session_channel="vk_wall",
        ),
    )

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "vk_wall", ConnectionState.LIVE)
    await repo.upsert_connection(p.id, "vk_channel", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    assert wall_calls[0] == 1
    assert channel_calls[0] == 1

    wall_conn = await repo.get_connection(p.id, "vk_wall")
    chan_conn = await repo.get_connection(p.id, "vk_channel")
    assert wall_conn.state == ConnectionState.NEEDS_RELOGIN
    assert chan_conn.state == ConnectionState.NEEDS_RELOGIN


# ── 8. Завершение фонового таска ──────────────────────────────────────────────


async def test_run_forever_stops_on_cancel():
    """CancelledError выходит из run_forever без подавления."""
    slept = [0]

    async def fast_sleep(seconds: float) -> None:
        slept[0] += 1
        if slept[0] >= 2:
            raise asyncio.CancelledError

    checker = HealthChecker.__new__(HealthChecker)
    checker._factory = None  # won't be called — run_once patched
    checker._poll = 0.0
    checker._sleep = fast_sleep

    # Patch run_once to do nothing
    checker.run_once = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await checker.run_forever()


async def test_run_forever_via_task_cancel():
    """asyncio.Task cancellation properly terminates run_forever."""

    async def _noop_run_once():
        pass

    checker = HealthChecker.__new__(HealthChecker)
    checker._poll = 100.0
    checker._sleep = asyncio.sleep
    checker.run_once = _noop_run_once

    task = asyncio.create_task(checker.run_forever())
    await asyncio.sleep(0)  # let it start
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.done()


# ── 9. Изоляция ошибок ────────────────────────────────────────────────────────


async def test_error_in_one_channel_does_not_stop_others(repo, factory, monkeypatch):
    """Исключение в одном канале не прерывает проверку остальных."""
    checked = []

    async def _boom(cred):
        raise RuntimeError("boom")

    async def _ok(cred):
        checked.append(True)
        return True

    monkeypatch.setitem(
        VALIDATORS,
        "telegram",
        ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_boom),
    )
    monkeypatch.setitem(
        VALIDATORS,
        "telegraph",
        ChannelValidatorDef(
            kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_ok, enabled=True
        ),
    )

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)
    await repo.upsert_connection(p.id, "telegraph", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    # telegraph was still checked (boom in telegram didn't abort)
    assert len(checked) == 1
    # telegram preserved (transient)
    assert (await repo.get_connection(p.id, "telegram")).state == ConnectionState.LIVE
    # telegraph still live (returned True)
    assert (await repo.get_connection(p.id, "telegraph")).state == ConnectionState.LIVE


async def test_error_in_one_profile_does_not_stop_others(factory, monkeypatch):
    """Исключение в одном профиле не прерывает проверку следующих."""
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    vault = Fernet(Fernet.generate_key())

    async with AsyncSession(engine, expire_on_commit=False) as s:
        repo = ProfileRepository(s, vault=vault)
        p1 = await repo.create_profile("bad")
        p2 = await repo.create_profile("good")
        await repo.upsert_connection(p1.id, "telegram", ConnectionState.LIVE)
        await repo.upsert_connection(p2.id, "telegraph", ConnectionState.LIVE)

    checked = []

    async def _ok(cred):
        checked.append(True)
        return True

    monkeypatch.setitem(
        VALIDATORS,
        "telegraph",
        ChannelValidatorDef(
            kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_ok, enabled=True
        ),
    )

    factory2 = async_sessionmaker(engine, expire_on_commit=False)
    checker = HealthChecker(factory2, poll_seconds=999)
    await checker.run_once()

    # telegraph on p2 was still checked
    assert len(checked) == 1

    await engine.dispose()
