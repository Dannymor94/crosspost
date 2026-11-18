"""Тесты HealthChecker и fail-closed dispatch (T-202 / T-202-R1).

Все тесты на моках: без реального браузера, сети или API.
VALIDATORS подменяется через monkeypatch.

R1-additions:
  • confirmed_dead contract (blockers 1 & 5)
  • secret sanitization (blocker 2)
  • CAS guard (blocker 3)
  • VK session dedup (blocker 4)
  • full dispatch path (blocker 6)
  • real validator paths with mocked network (telegra.ph, Instagram)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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

    return get_vault()


@pytest_asyncio.fixture
async def repo(session, vault) -> ProfileRepository:
    return ProfileRepository(session, vault=vault)


@pytest_asyncio.fixture
async def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


# confirmed_dead=True triggers NEEDS_RELOGIN; confirmed_dead=False (or plain bool) = transient
def _make_def(alive: bool) -> ChannelValidatorDef:
    async def _fn(credential: str | None) -> ValidatorResult:
        return ValidatorResult(alive=alive, confirmed_dead=not alive)

    return ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_fn)


def _make_raising_def(exc: Exception) -> ChannelValidatorDef:
    async def _fn(credential: str | None) -> ValidatorResult:
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
        VALIDATORS,
        "telegram",
        _make_result_def(ValidatorResult(alive=False, error="expired", confirmed_dead=True)),
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
    assert conn.state == ConnectionState.LIVE


async def test_transient_error_preserves_needs_relogin_state(repo, factory, monkeypatch):
    monkeypatch.setitem(VALIDATORS, "telegram", _make_raising_def(OSError("network unreachable")))

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.NEEDS_RELOGIN)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "telegram")
    assert conn.state == ConnectionState.NEEDS_RELOGIN


async def test_unknown_result_preserves_state(repo, factory, monkeypatch):
    """ValidatorResult(alive=False, confirmed_dead=False) = transient/unknown → preserve."""
    monkeypatch.setitem(
        VALIDATORS,
        "telegram",
        _make_result_def(ValidatorResult(alive=False, confirmed_dead=False)),
    )

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "telegram")
    assert conn.state == ConnectionState.LIVE  # preserved — not confirmed dead


# ── 3. Забаненное подключение → не трогается ──────────────────────────────────


async def test_banned_connection_never_overwritten(repo, factory, monkeypatch):
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
        return ValidatorResult(alive=True)

    monkeypatch.setitem(
        VALIDATORS,
        "telegram",
        ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_fn),
    )

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.BANNED)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    assert called == []


# ── 3b. CAS guard — set_needs_relogin_if_live ────────────────────────────────


async def test_set_needs_relogin_if_live_updates_live(repo):
    """CAS: state=LIVE → update succeeds, returns True."""
    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)

    applied = await repo.set_needs_relogin_if_live(p.id, "telegram")
    assert applied is True
    conn = await repo.get_connection(p.id, "telegram")
    assert conn.state == ConnectionState.NEEDS_RELOGIN


async def test_set_needs_relogin_if_live_does_not_overwrite_banned(repo):
    """CAS: state=BANNED → update skipped, returns False, state stays BANNED."""
    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.BANNED)

    applied = await repo.set_needs_relogin_if_live(p.id, "telegram")
    assert applied is False
    conn = await repo.get_connection(p.id, "telegram")
    assert conn.state == ConnectionState.BANNED


async def test_cas_guard_via_health_checker(repo, factory, monkeypatch):
    """When state changes to BANNED while probe runs, CAS prevents overwrite."""
    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)

    # Simulate: probe returns confirmed_dead=True but state is now BANNED (changed externally)
    # We bang the state to BANNED before checker writes, then verify it stays BANNED.
    async def _confirmed_dead_fn(cred: str | None) -> ValidatorResult:
        # Simulate concurrent state change while probe was running
        await repo.upsert_connection(p.id, "telegram", ConnectionState.BANNED)
        return ValidatorResult(alive=False, confirmed_dead=True)

    monkeypatch.setitem(
        VALIDATORS,
        "telegram",
        ChannelValidatorDef(
            kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_confirmed_dead_fn
        ),
    )

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "telegram")
    # CAS prevented overwrite: BANNED must survive
    assert conn.state == ConnectionState.BANNED


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

    store = CheckpointStore(repo.session, profile_id=p.id)
    adapter = await build_profile_adapter(repo, p.id, "telegraph", store=store)

    assert isinstance(adapter, TelegraphAdapter)


# ── 6. Несколько профилей → нет межпрофильных изменений ──────────────────────


async def test_multiple_profiles_isolated(repo, factory, monkeypatch):
    async def _alice_fn(cred):
        return ValidatorResult(alive=False, confirmed_dead=True)

    async def _bob_fn(cred):
        return ValidatorResult(alive=True)

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
    assert alice_conn.state == ConnectionState.NEEDS_RELOGIN
    assert bob_conn.state == ConnectionState.LIVE

    assert await repo.get_connection(alice.id, "telegraph") is None
    assert await repo.get_connection(bob.id, "telegram") is None


# ── 7. VK shared session — dedup logic ────────────────────────────────────────


async def test_vk_channel_uses_vk_wall_session(repo, factory, monkeypatch, vault):
    """vk_channel.session_channel='vk_wall' → health check probes vk_wall session once."""
    received_credentials: list[str | None] = []

    async def _capture_fn(credential: str | None) -> ValidatorResult:
        received_credentials.append(credential)
        return ValidatorResult(alive=True)

    # With dedup, session_key="vk_wall" → primary probe uses VALIDATORS["vk_wall"].fn
    monkeypatch.setitem(
        VALIDATORS,
        "vk_wall",
        ChannelValidatorDef(
            kind="browser",
            credential_kind=CredentialKind.STORAGE_STATE,
            fn=_capture_fn,
        ),
    )
    monkeypatch.setitem(
        VALIDATORS,
        "vk_channel",
        ChannelValidatorDef(
            kind="browser",
            credential_kind=CredentialKind.STORAGE_STATE,
            fn=AsyncMock(return_value=ValidatorResult(alive=True)),
            session_channel="vk_wall",
        ),
    )

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "vk_wall", CredentialKind.STORAGE_STATE, "vk_session_data")
    await repo.upsert_connection(p.id, "vk_channel", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    # vk_wall fn was called with vk_wall credential (not vk_channel fn)
    assert received_credentials == ["vk_session_data"]


async def test_vk_shared_session_probed_once(repo, factory, monkeypatch):
    """vk_wall and vk_channel share session_key 'vk_wall' → ONE probe, both affected."""
    probe_calls = [0]

    async def _wall_fn(cred):
        probe_calls[0] += 1
        return ValidatorResult(alive=False, confirmed_dead=True)

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
            fn=AsyncMock(return_value=ValidatorResult(alive=False, confirmed_dead=True)),
            session_channel="vk_wall",
        ),
    )

    p = await repo.create_profile("alice")
    # Store vk_wall credential (used by both via dedup)
    await repo.set_credential(p.id, "vk_wall", CredentialKind.STORAGE_STATE, "vk_session")
    await repo.upsert_connection(p.id, "vk_wall", ConnectionState.LIVE)
    await repo.upsert_connection(p.id, "vk_channel", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    assert probe_calls[0] == 1  # only one network probe

    wall_conn = await repo.get_connection(p.id, "vk_wall")
    chan_conn = await repo.get_connection(p.id, "vk_channel")
    assert wall_conn.state == ConnectionState.NEEDS_RELOGIN
    assert chan_conn.state == ConnectionState.NEEDS_RELOGIN


async def test_vk_banned_channel_not_affected_by_session_result(repo, factory, monkeypatch):
    """vk_wall LIVE, vk_channel BANNED → session dead → only vk_wall → NEEDS_RELOGIN."""

    async def _dead_fn(cred):
        return ValidatorResult(alive=False, confirmed_dead=True)

    monkeypatch.setitem(
        VALIDATORS,
        "vk_wall",
        ChannelValidatorDef(
            kind="browser", credential_kind=CredentialKind.STORAGE_STATE, fn=_dead_fn
        ),
    )
    monkeypatch.setitem(
        VALIDATORS,
        "vk_channel",
        ChannelValidatorDef(
            kind="browser",
            credential_kind=CredentialKind.STORAGE_STATE,
            fn=AsyncMock(return_value=ValidatorResult(alive=False, confirmed_dead=True)),
            session_channel="vk_wall",
        ),
    )

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "vk_wall", CredentialKind.STORAGE_STATE, "vk_session")
    await repo.upsert_connection(p.id, "vk_wall", ConnectionState.LIVE)
    await repo.upsert_connection(p.id, "vk_channel", ConnectionState.BANNED)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    wall_conn = await repo.get_connection(p.id, "vk_wall")
    chan_conn = await repo.get_connection(p.id, "vk_channel")
    assert wall_conn.state == ConnectionState.NEEDS_RELOGIN
    assert chan_conn.state == ConnectionState.BANNED  # CAS protects BANNED


# ── 8. Завершение фонового таска ──────────────────────────────────────────────


async def test_run_forever_stops_on_cancel():
    slept = [0]

    async def fast_sleep(seconds: float) -> None:
        slept[0] += 1
        if slept[0] >= 2:
            raise asyncio.CancelledError

    checker = HealthChecker.__new__(HealthChecker)
    checker._factory = None
    checker._poll = 0.0
    checker._sleep = fast_sleep
    checker.run_once = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await checker.run_forever()


async def test_run_forever_via_task_cancel():
    async def _noop_run_once():
        pass

    checker = HealthChecker.__new__(HealthChecker)
    checker._poll = 100.0
    checker._sleep = asyncio.sleep
    checker.run_once = _noop_run_once

    task = asyncio.create_task(checker.run_forever())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.done()


# ── 9. Изоляция ошибок ────────────────────────────────────────────────────────


async def test_error_in_one_channel_does_not_stop_others(repo, factory, monkeypatch):
    checked = []

    async def _boom(cred):
        raise RuntimeError("boom")

    async def _ok(cred):
        checked.append(True)
        return ValidatorResult(alive=True)

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

    assert len(checked) == 1
    assert (await repo.get_connection(p.id, "telegram")).state == ConnectionState.LIVE
    assert (await repo.get_connection(p.id, "telegraph")).state == ConnectionState.LIVE


async def test_error_in_one_profile_does_not_stop_others(factory, monkeypatch):
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
        return ValidatorResult(alive=True)

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

    assert len(checked) == 1

    await engine.dispose()


# ── 10. Secret sanitization ───────────────────────────────────────────────────


async def test_transient_error_does_not_log_secret(repo, factory, monkeypatch, caplog):
    """Secret in exception message must not appear in DB logs or application logs."""
    secret = "secret_token_abcdef123"

    async def _fn(cred):
        raise RuntimeError(f"connection failed with token={secret}")

    monkeypatch.setitem(
        VALIDATORS,
        "telegram",
        ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_fn),
    )

    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)

    with caplog.at_level("DEBUG", logger="crosspost"):
        checker = HealthChecker(factory, poll_seconds=999)
        await checker.run_once()

    # Secret must not appear in application logs
    assert secret not in caplog.text

    # Secret must not appear in DB log messages
    from sqlalchemy import select as sa_select

    from crosspost.db.models import Log

    async with factory() as session:
        result = await session.execute(sa_select(Log).where(Log.profile_id == p.id))
        for log_row in result.scalars().all():
            assert secret not in log_row.message, f"Secret found in DB log: {log_row.message!r}"

    # State preserved (transient)
    conn = await repo.get_connection(p.id, "telegram")
    assert conn.state == ConnectionState.LIVE


# ── 11. Real validator paths — Telegraph ──────────────────────────────────────


async def test_telegraph_network_failure_preserves_state(repo, factory, monkeypatch):
    """ConnectError in real _validate_telegraph → confirmed_dead=False → LIVE preserved."""
    import httpx

    from crosspost.channels.validators import _validate_telegraph

    class _FailingClient:
        def __init__(self, **kwargs):
            pass

        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)

    monkeypatch.setitem(
        VALIDATORS,
        "telegraph",
        ChannelValidatorDef(
            kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_validate_telegraph
        ),
    )

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegraph", CredentialKind.API_TOKEN, "tok")
    await repo.upsert_connection(p.id, "telegraph", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "telegraph")
    assert conn.state == ConnectionState.LIVE  # transient → preserved


async def test_telegraph_auth_failure_sets_needs_relogin(repo, factory, monkeypatch):
    """ok=False response from real _validate_telegraph → confirmed_dead=True → NEEDS_RELOGIN."""
    import httpx

    from crosspost.channels.validators import _validate_telegraph

    class _MockResponse:
        status_code = 200

        def json(self):
            return {"ok": False, "error": "INVALID_ACCESS_TOKEN"}

    class _MockClient:
        def __init__(self, **kwargs):
            pass

        async def get(self, *args, **kwargs):
            return _MockResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    monkeypatch.setitem(
        VALIDATORS,
        "telegraph",
        ChannelValidatorDef(
            kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_validate_telegraph
        ),
    )

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegraph", CredentialKind.API_TOKEN, "bad_tok")
    await repo.upsert_connection(p.id, "telegraph", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "telegraph")
    assert conn.state == ConnectionState.NEEDS_RELOGIN


# ── 12. Real validator paths — Instagram ──────────────────────────────────────


async def test_instagram_proxy_error_preserves_state(repo, factory, monkeypatch):
    """ProxyError in real _validate_instagram → confirmed_dead=False → LIVE preserved."""
    import httpx

    from crosspost.channels.validators import _validate_instagram

    class _MockAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            raise httpx.ProxyError("proxy unreachable")

    monkeypatch.setattr(httpx, "AsyncClient", _MockAsyncClient)

    import json

    blob = json.dumps({"access_token": "tok", "ig_user_id": "123"})

    monkeypatch.setitem(
        VALIDATORS,
        "instagram",
        ChannelValidatorDef(
            kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_validate_instagram
        ),
    )

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "instagram", CredentialKind.API_TOKEN, blob)
    await repo.upsert_connection(p.id, "instagram", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "instagram")
    assert conn.state == ConnectionState.LIVE


async def test_instagram_token_error_sets_needs_relogin(repo, factory, monkeypatch):
    """token_error from real _validate_instagram → confirmed_dead=True → NEEDS_RELOGIN."""
    import json

    import httpx

    from crosspost.channels.validators import _validate_instagram

    class _MockResponse:
        status_code = 200

        def json(self):
            return {
                "error": {
                    "message": "Invalid OAuth access token",
                    "type": "OAuthException",
                    "code": 190,
                }
            }

    class _MockAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            return _MockResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _MockAsyncClient)

    blob = json.dumps({"access_token": "expired_tok", "ig_user_id": "123"})

    monkeypatch.setitem(
        VALIDATORS,
        "instagram",
        ChannelValidatorDef(
            kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_validate_instagram
        ),
    )

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "instagram", CredentialKind.API_TOKEN, blob)
    await repo.upsert_connection(p.id, "instagram", ConnectionState.LIVE)

    checker = HealthChecker(factory, poll_seconds=999)
    await checker.run_once()

    conn = await repo.get_connection(p.id, "instagram")
    assert conn.state == ConnectionState.NEEDS_RELOGIN


# ── 13. Full dispatch path ────────────────────────────────────────────────────


async def test_dispatch_blocked_for_needs_relogin(repo, monkeypatch):
    """Full chain: NEEDS_RELOGIN connection → PublishService.publish returns needs_relogin."""
    from crosspost.content.canonical import CanonicalContent, ContentType
    from crosspost.orchestrator.publish_service import PublishService

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegraph", CredentialKind.API_TOKEN, "tok")
    await repo.upsert_connection(p.id, "telegraph", ConnectionState.NEEDS_RELOGIN)

    pub_repo = MagicMock()
    pub_repo.set_status = AsyncMock()
    pub_repo.is_done = AsyncMock(return_value=False)
    pub_repo.get_status = AsyncMock(return_value=None)

    async def adapter_factory(channel: str, target_id: str):
        return await build_profile_adapter(repo, p.id, channel, target_id)

    service = PublishService(pub_repo, adapter_factory)
    content = CanonicalContent(type=ContentType.ARTICLE, text="hello", title="Title")
    outcomes = await service.publish(content, ["telegraph"], publication_id="test-pub-001")

    assert len(outcomes) == 1
    assert outcomes[0].status == "needs_relogin"


async def test_dispatch_blocked_for_banned(repo, monkeypatch):
    """Full chain: BANNED connection → PublishService.publish returns needs_relogin."""
    from crosspost.content.canonical import CanonicalContent, ContentType
    from crosspost.orchestrator.publish_service import PublishService

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegraph", CredentialKind.API_TOKEN, "tok")
    await repo.upsert_connection(p.id, "telegraph", ConnectionState.BANNED)

    pub_repo = MagicMock()
    pub_repo.set_status = AsyncMock()
    pub_repo.is_done = AsyncMock(return_value=False)
    pub_repo.get_status = AsyncMock(return_value=None)

    async def adapter_factory(channel: str, target_id: str):
        return await build_profile_adapter(repo, p.id, channel, target_id)

    service = PublishService(pub_repo, adapter_factory)
    content = CanonicalContent(type=ContentType.ARTICLE, text="hello", title="Title")
    outcomes = await service.publish(content, ["telegraph"], publication_id="test-pub-002")

    assert len(outcomes) == 1
    assert outcomes[0].status == "needs_relogin"


async def test_dispatch_live_connection_reaches_adapter(repo, monkeypatch):
    """Full chain: LIVE connection with real credential → adapter is built (not None)."""
    from crosspost.adapters.api.telegraph import TelegraphAdapter
    from crosspost.db.sqlite_store import CheckpointStore

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegraph", CredentialKind.API_TOKEN, "tok123")
    await repo.upsert_connection(p.id, "telegraph", ConnectionState.LIVE)

    store = CheckpointStore(repo.session, profile_id=p.id)
    adapter = await build_profile_adapter(repo, p.id, "telegraph", store=store)

    # LIVE → adapter is built, not None
    assert isinstance(adapter, TelegraphAdapter)
