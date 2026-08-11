"""Тесты роута глобальных настроек (видимость браузера).

Ключевое:
  - только админ читает/меняет (client → 403, аноним → 401);
  - PUT задаёт override, GET его отражает, значение переживает «перезапуск» (файл).
"""

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
from crosspost.db.user_repo import UserRepository
from crosspost.web import deps
from crosspost.web.routes import auth, settings


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "test-secret-please-change")
    monkeypatch.setenv("RUNTIME_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("BROWSER_HEADLESS", "false")  # детерминированный дефолт без override


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(f)
    yield f
    await engine.dispose()


@pytest_asyncio.fixture
async def app(factory) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(a: FastAPI) -> AsyncGenerator[None, None]:
        yield

    a = FastAPI(lifespan=_lifespan)
    a.include_router(auth.router)
    a.include_router(settings.router)
    return a


@pytest_asyncio.fixture
async def seed(factory) -> None:
    async with factory() as s:
        urepo = UserRepository(s)
        await urepo.create_user("admin", "adminpass", role=Role.ADMIN)
        await urepo.create_user("alice", "alicepass", role=Role.CLIENT, profile_id=None)


async def _login(app: FastAPI, login: str, password: str) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    r = await c.post("/api/auth/login", json={"login": login, "password": password})
    assert r.status_code == 200, r.text
    return c


def _anon(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_anonymous_blocked(app, seed):
    c = _anon(app)
    try:
        assert (await c.get("/api/settings/browser")).status_code == 401
    finally:
        await c.aclose()


async def test_client_forbidden(app, seed):
    c = await _login(app, "alice", "alicepass")
    try:
        assert (await c.get("/api/settings/browser")).status_code == 403
        r = await c.put("/api/settings/browser", json={"headless": True})
        assert r.status_code == 403
    finally:
        await c.aclose()


async def test_admin_get_default_is_env(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        r = await c.get("/api/settings/browser")
        assert r.status_code == 200
        assert r.json() == {"headless": False}  # из BROWSER_HEADLESS=false, override нет
    finally:
        await c.aclose()


async def test_admin_put_toggles_and_persists(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        r = await c.put("/api/settings/browser", json={"headless": True})
        assert r.status_code == 200
        assert r.json() == {"headless": True}
        # override отражается на GET и переживёт рестарт (лежит в файле)
        assert (await c.get("/api/settings/browser")).json() == {"headless": True}

        r = await c.put("/api/settings/browser", json={"headless": False})
        assert r.json() == {"headless": False}
        assert (await c.get("/api/settings/browser")).json() == {"headless": False}
    finally:
        await c.aclose()
