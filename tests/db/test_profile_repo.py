"""Тесты ProfileRepository: CRUD профилей, подключений, учёток, изоляция.

Ключевой сценарий: данные профиля A физически недоступны при запросе от profile_id B.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ConnectionState, CredentialKind
from crosspost.db.profile_repo import ProfileRepository


@pytest_asyncio.fixture
async def session():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest.fixture
def vault() -> Fernet:
    return Fernet(Fernet.generate_key())


@pytest_asyncio.fixture
async def repo(session, vault) -> ProfileRepository:
    return ProfileRepository(session, vault=vault)


# ── Profiles ──────────────────────────────────────────────────────────────────


async def test_create_and_get_profile(repo):
    p = await repo.create_profile("alice")
    assert p.id is not None
    assert p.name == "alice"

    fetched = await repo.get_profile(p.id)
    assert fetched is not None
    assert fetched.name == "alice"


async def test_list_profiles(repo):
    await repo.create_profile("alice")
    await repo.create_profile("bob")
    profiles = await repo.list_profiles()
    names = [p.name for p in profiles]
    assert "alice" in names
    assert "bob" in names


async def test_duplicate_profile_name_raises(repo):
    await repo.create_profile("alice")
    with pytest.raises(IntegrityError):
        await repo.create_profile("alice")


async def test_get_nonexistent_profile_returns_none(repo):
    result = await repo.get_profile(9999)
    assert result is None


# ── Connections ───────────────────────────────────────────────────────────────


async def test_upsert_connection_creates(repo):
    p = await repo.create_profile("alice")
    conn = await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)
    assert conn.state == ConnectionState.LIVE
    assert conn.channel == "telegram"
    assert conn.profile_id == p.id


async def test_upsert_connection_updates_state(repo):
    p = await repo.create_profile("alice")
    await repo.upsert_connection(p.id, "telegram", ConnectionState.LIVE)
    conn = await repo.upsert_connection(p.id, "telegram", ConnectionState.NEEDS_RELOGIN)
    assert conn.state == ConnectionState.NEEDS_RELOGIN


async def test_get_connections_returns_only_own(repo):
    a = await repo.create_profile("alice")
    b = await repo.create_profile("bob")
    await repo.upsert_connection(a.id, "telegram", ConnectionState.LIVE)
    await repo.upsert_connection(b.id, "vk", ConnectionState.BANNED)

    a_conns = await repo.get_connections(a.id)
    assert len(a_conns) == 1
    assert a_conns[0].channel == "telegram"

    b_conns = await repo.get_connections(b.id)
    assert len(b_conns) == 1
    assert b_conns[0].channel == "vk"


async def test_get_connection_returns_none_for_missing(repo):
    p = await repo.create_profile("alice")
    conn = await repo.get_connection(p.id, "nonexistent")
    assert conn is None


# ── Credentials ───────────────────────────────────────────────────────────────


async def test_set_and_get_credential_roundtrip(repo):
    p = await repo.create_profile("alice")
    secret = "tg-session-string-abc123"
    await repo.set_credential(p.id, "telegram", CredentialKind.API_TOKEN, secret)
    result = await repo.get_credential(p.id, "telegram", CredentialKind.API_TOKEN)
    assert result == secret


async def test_set_credential_upserts(repo):
    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegram", CredentialKind.API_TOKEN, "old-token")
    await repo.set_credential(p.id, "telegram", CredentialKind.API_TOKEN, "new-token")
    result = await repo.get_credential(p.id, "telegram", CredentialKind.API_TOKEN)
    assert result == "new-token"


async def test_get_credential_returns_none_when_missing(repo):
    p = await repo.create_profile("alice")
    result = await repo.get_credential(p.id, "telegram", CredentialKind.API_TOKEN)
    assert result is None


# ── Изоляция профилей (главный тест) ─────────────────────────────────────────


async def test_connection_isolation_between_profiles(repo):
    """Подключения профиля A недоступны при запросе от profile_id B."""
    a = await repo.create_profile("alice")
    b = await repo.create_profile("bob")

    await repo.upsert_connection(a.id, "telegram", ConnectionState.LIVE)
    await repo.upsert_connection(b.id, "telegram", ConnectionState.BANNED)

    a_conn = await repo.get_connection(a.id, "telegram")
    b_conn = await repo.get_connection(b.id, "telegram")

    assert a_conn is not None and a_conn.state == ConnectionState.LIVE
    assert b_conn is not None and b_conn.state == ConnectionState.BANNED
    # физически разные строки
    assert a_conn.id != b_conn.id


async def test_credential_isolation_between_profiles(repo):
    """Учётки профиля A недоступны при запросе от profile_id B."""
    a = await repo.create_profile("alice")
    b = await repo.create_profile("bob")

    await repo.set_credential(a.id, "telegram", CredentialKind.API_TOKEN, "alice-secret")
    await repo.set_credential(b.id, "telegram", CredentialKind.API_TOKEN, "bob-secret")

    a_token = await repo.get_credential(a.id, "telegram", CredentialKind.API_TOKEN)
    b_token = await repo.get_credential(b.id, "telegram", CredentialKind.API_TOKEN)

    assert a_token == "alice-secret"
    assert b_token == "bob-secret"
    # явная проверка: запрос от B не возвращает данные A
    assert a_token != b_token


async def test_cross_profile_credential_not_visible(repo):
    """get_credential с чужим profile_id возвращает None, не чужой секрет."""
    a = await repo.create_profile("alice")
    b = await repo.create_profile("bob")

    await repo.set_credential(a.id, "telegram", CredentialKind.API_TOKEN, "alice-only")

    # bob запрашивает ту же пару channel+kind — должен получить None
    result = await repo.get_credential(b.id, "telegram", CredentialKind.API_TOKEN)
    assert result is None


async def test_cross_profile_connections_not_visible(repo):
    """get_connections с чужим profile_id возвращает пустой список."""
    a = await repo.create_profile("alice")
    b = await repo.create_profile("bob")

    await repo.upsert_connection(a.id, "telegram", ConnectionState.LIVE)
    await repo.upsert_connection(a.id, "vk", ConnectionState.LIVE)

    # bob не имеет подключений — список пустой, не виден чужой
    b_conns = await repo.get_connections(b.id)
    assert b_conns == []


# ── Delete credential / connection (сброс входа) ───────────────────────────────


async def test_delete_credential_removes_it(repo):
    a = await repo.create_profile("alice")
    await repo.set_credential(a.id, "telegram", CredentialKind.API_TOKEN, "secret")
    assert await repo.get_credential(a.id, "telegram", CredentialKind.API_TOKEN) == "secret"

    await repo.delete_credential(a.id, "telegram", CredentialKind.API_TOKEN)
    assert await repo.get_credential(a.id, "telegram", CredentialKind.API_TOKEN) is None


async def test_delete_credential_is_idempotent(repo):
    a = await repo.create_profile("alice")
    # Нет учётки — удаление не падает.
    await repo.delete_credential(a.id, "telegram", CredentialKind.API_TOKEN)


async def test_delete_connection_makes_channel_not_connected(repo):
    a = await repo.create_profile("alice")
    await repo.upsert_connection(a.id, "vk_wall", ConnectionState.LIVE)
    assert await repo.get_connection(a.id, "vk_wall") is not None

    await repo.delete_connection(a.id, "vk_wall")
    assert await repo.get_connection(a.id, "vk_wall") is None


async def test_delete_credential_isolated_to_profile(repo):
    a = await repo.create_profile("alice")
    b = await repo.create_profile("bob")
    await repo.set_credential(a.id, "telegram", CredentialKind.API_TOKEN, "a-secret")
    await repo.set_credential(b.id, "telegram", CredentialKind.API_TOKEN, "b-secret")

    await repo.delete_credential(a.id, "telegram", CredentialKind.API_TOKEN)

    # У B учётка на месте — сброс у A её не тронул.
    assert await repo.get_credential(a.id, "telegram", CredentialKind.API_TOKEN) is None
    assert await repo.get_credential(b.id, "telegram", CredentialKind.API_TOKEN) == "b-secret"
