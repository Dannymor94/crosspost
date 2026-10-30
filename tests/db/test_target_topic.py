"""Tests for topic_id / peer_access_hash columns on channel_targets — RED until migration."""

from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ChannelTarget
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.target_repo import ChannelTargetRepository
from crosspost.db.vault import get_vault

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


@pytest_asyncio.fixture
async def pid(factory):
    async with factory() as s:
        p = await ProfileRepository(s, vault=get_vault()).create_profile("test")
    return p.id


async def test_columns_exist(factory):
    """Both new columns must exist in the schema."""
    async with factory() as s:
        result = await s.execute(text("PRAGMA table_info(channel_targets)"))
        cols = {row[1] for row in result.fetchall()}
    assert "topic_id" in cols
    assert "peer_access_hash" in cols


async def test_new_target_defaults_null(factory, pid):
    """Existing targets get null for both new columns."""
    async with factory() as s:
        t = ChannelTarget(
            profile_id=pid,
            channel="telegram",
            name="Main",
            value_blob="x",
        )
        s.add(t)
        await s.commit()
        await s.refresh(t)

    assert t.topic_id is None
    assert t.peer_access_hash is None


async def test_set_topic_id(factory, pid):
    """set_topic_id stores and retrieves a topic id."""
    async with factory() as s:
        repo = ChannelTargetRepository(s, vault=get_vault(), profile_id=pid)
        t = await repo.add_target("telegram", "Chan", "@chan")
        updated = await repo.set_topic_id(t.id, 42)
        assert updated is True
        refreshed = await repo.get_target(t.id)
        assert refreshed.topic_id == 42


async def test_set_topic_id_none(factory, pid):
    """set_topic_id(None) clears the topic."""
    async with factory() as s:
        repo = ChannelTargetRepository(s, vault=get_vault(), profile_id=pid)
        t = await repo.add_target("telegram", "Chan", "@chan")
        await repo.set_topic_id(t.id, 99)
        await repo.set_topic_id(t.id, None)
        refreshed = await repo.get_target(t.id)
        assert refreshed.topic_id is None
