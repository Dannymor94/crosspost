"""Tests for topic_id in create/patch target API — RED until implementation."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import Role
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.user_repo import UserRepository
from crosspost.db.vault import get_vault
from crosspost.web import deps
from crosspost.web.routes import channels, profiles

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(f)
    yield f
    await engine.dispose()


@pytest_asyncio.fixture
async def client(factory) -> AsyncGenerator[AsyncClient, None]:
    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    for r in (profiles, channels):
        app.include_router(r.router)

    from crosspost.auth.sessions import sign_session

    async with factory() as s:
        admin = await UserRepository(s).create_user("admin", "adminpass", role=Role.ADMIN)
        admin_id = admin.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("session", sign_session(admin_id))
        yield c


async def _profile(factory, name="acme") -> int:
    async with factory() as s:
        return (await ProfileRepository(s, vault=get_vault()).create_profile(name)).id


# ── create_target with topic_id ───────────────────────────────────────────────


async def test_create_tg_target_with_topic_id(client, factory):
    pid = await _profile(factory)
    r = await client.post(
        f"/api/profiles/{pid}/channels/telegram/targets",
        json={"name": "Forum topic", "value": "@mygroup", "topic_id": 42},
    )
    assert r.status_code == 200, r.text
    assert r.json()["topic_id"] == 42


async def test_create_tg_target_topic_1_stored_as_null(client, factory):
    """topic_id == 1 (General) must be coerced to null."""
    pid = await _profile(factory)
    r = await client.post(
        f"/api/profiles/{pid}/channels/telegram/targets",
        json={"name": "General", "value": "@mygroup", "topic_id": 1},
    )
    assert r.status_code == 200, r.text
    assert r.json()["topic_id"] is None


async def test_create_tg_target_no_topic_id(client, factory):
    pid = await _profile(factory)
    r = await client.post(
        f"/api/profiles/{pid}/channels/telegram/targets",
        json={"name": "Chan", "value": "@chan"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["topic_id"] is None


async def test_topic_id_rejected_for_non_telegram(client, factory):
    pid = await _profile(factory)
    r = await client.post(
        f"/api/profiles/{pid}/channels/vk_wall/targets",
        json={"name": "Группа", "value": "yogapiecom", "topic_id": 5},
    )
    assert r.status_code == 422


async def test_extra_field_ignored(client, factory):
    """Unrelated extra fields are silently ignored (Pydantic default behavior)."""
    pid = await _profile(factory)
    r = await client.post(
        f"/api/profiles/{pid}/channels/telegram/targets",
        json={"name": "Chan", "value": "@chan", "unknown_field": "whatever"},
    )
    assert r.status_code == 200, r.text


async def test_peer_access_hash_rejected_from_api(client, factory):
    """peer_access_hash must return 422 — it is set by discovery only."""
    pid = await _profile(factory)
    r = await client.post(
        f"/api/profiles/{pid}/channels/telegram/targets",
        json={"name": "Chan", "value": "@chan", "peer_access_hash": 99999},
    )
    assert r.status_code == 422
    assert r.json() == {"detail": "Проверьте поля формы"}
    assert "99999" not in r.text


# ── patch_target with topic_id ────────────────────────────────────────────────


async def test_patch_topic_id(client, factory):
    pid = await _profile(factory)
    created = await client.post(
        f"/api/profiles/{pid}/channels/telegram/targets",
        json={"name": "Chan", "value": "@chan"},
    )
    tid = created.json()["id"]

    r = await client.patch(
        f"/api/profiles/{pid}/channels/telegram/targets/{tid}",
        json={"topic_id": 99},
    )
    assert r.status_code == 200, r.text
    assert r.json()["topic_id"] == 99


async def test_patch_name_only_preserves_topic_id(client, factory):
    """PATCH with only name → topic_id must be unchanged (omitted ≠ null)."""
    pid = await _profile(factory)
    created = await client.post(
        f"/api/profiles/{pid}/channels/telegram/targets",
        json={"name": "Chan", "value": "@chan", "topic_id": 285},
    )
    tid = created.json()["id"]

    r = await client.patch(
        f"/api/profiles/{pid}/channels/telegram/targets/{tid}",
        json={"name": "Renamed"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["topic_id"] == 285


async def test_patch_topic_id_clears_with_null(client, factory):
    pid = await _profile(factory)
    created = await client.post(
        f"/api/profiles/{pid}/channels/telegram/targets",
        json={"name": "Chan", "value": "@chan", "topic_id": 42},
    )
    tid = created.json()["id"]

    r = await client.patch(
        f"/api/profiles/{pid}/channels/telegram/targets/{tid}",
        json={"topic_id": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["topic_id"] is None
