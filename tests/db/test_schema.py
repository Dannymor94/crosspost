"""Тесты ORM-модели: CRUD на profiles, connections, credentials, logs.

Покрываем:
  - Profile создаётся, читается
  - Connection создаётся со state live, меняется на needs_relogin
  - Credential сохраняет blob (пока plain, шифрование в 0.2)
  - Log записывается, читается по profile+channel
  - Каскадное удаление: удалил profile → его connections/credentials/publications/logs исчезли
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import (
    Connection,
    ConnectionState,
    Credential,
    CredentialKind,
    Log,
    Profile,
)


@pytest_asyncio.fixture
async def session():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def profile(session):
    p = Profile(name="medithou")
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p


# ── Profile ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_profile_created(session, profile):
    row = await session.get(Profile, profile.id)
    assert row is not None
    assert row.name == "medithou"


# ── Connection ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connection_default_live(session, profile):
    conn = Connection(profile_id=profile.id, channel="telegram")
    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    assert conn.state == ConnectionState.LIVE


@pytest.mark.asyncio
async def test_connection_state_update(session, profile):
    conn = Connection(profile_id=profile.id, channel="yandex")
    session.add(conn)
    await session.commit()

    conn.state = ConnectionState.NEEDS_RELOGIN
    await session.commit()
    await session.refresh(conn)
    assert conn.state == ConnectionState.NEEDS_RELOGIN


# ── Credential ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_credential_stores_blob(session, profile):
    cred = Credential(
        profile_id=profile.id,
        channel="telegram",
        kind=CredentialKind.API_TOKEN,
        blob="session-string-here",
    )
    session.add(cred)
    await session.commit()
    await session.refresh(cred)
    assert cred.blob == "session-string-here"
    assert cred.kind == CredentialKind.API_TOKEN


# ── Log ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_created(session, profile):
    log = Log(
        profile_id=profile.id,
        channel="vk_wall",
        publication_id="pub-99",
        level="ERROR",
        message="таймаут модалки",
    )
    session.add(log)
    await session.commit()

    rows = (
        (
            await session.execute(
                select(Log).where(Log.profile_id == profile.id, Log.channel == "vk_wall")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].message == "таймаут модалки"


# ── Cascade delete ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cascade_delete_profile(session, profile):
    conn = Connection(profile_id=profile.id, channel="telegram")
    cred = Credential(
        profile_id=profile.id, channel="telegram", kind=CredentialKind.STORAGE_STATE, blob="x"
    )
    log = Log(
        profile_id=profile.id,
        channel="telegram",
        publication_id="pub-0",
        level="INFO",
        message="ok",
    )
    session.add_all([conn, cred, log])
    await session.commit()

    await session.delete(profile)
    await session.commit()

    assert (
        await session.execute(select(Connection).where(Connection.profile_id == profile.id))
    ).first() is None
    assert (
        await session.execute(select(Credential).where(Credential.profile_id == profile.id))
    ).first() is None
    assert (await session.execute(select(Log).where(Log.profile_id == profile.id))).first() is None
