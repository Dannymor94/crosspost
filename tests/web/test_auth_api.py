"""Тесты авторизации и изоляции по ролям (Часть 1).

Ключевое:
  - client A НЕ видит и НЕ может запросить профиль B (403) — на бэкенде;
  - admin видит оба профиля;
  - без входа любой profile-scoped роут → 401;
  - admin заводит client-аккаунт; клиент входит и оперирует только своим профилем.
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
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.user_repo import UserRepository
from crosspost.db.vault import get_vault
from crosspost.web import deps
from crosspost.web.routes import auth, channels, profiles, publish, tg_login, users


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "test-secret-please-change")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))


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
    for r in (auth, users, profiles, channels, tg_login, publish):
        a.include_router(r.router)
    return a


@pytest_asyncio.fixture
async def seed(factory) -> dict[str, int]:
    """Два профиля (A, B), admin, client alice→A. Возвращает id-шники."""
    async with factory() as s:
        prepo = ProfileRepository(s, vault=get_vault())
        a = await prepo.create_profile("client-A")
        b = await prepo.create_profile("client-B")
        urepo = UserRepository(s)
        admin = await urepo.create_user("admin", "adminpass", role=Role.ADMIN)
        alice = await urepo.create_user("alice", "alicepass", role=Role.CLIENT, profile_id=a.id)
    return {"a": a.id, "b": b.id, "admin": admin.id, "alice": alice.id}


async def _login(app: FastAPI, login: str, password: str) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    r = await c.post("/api/auth/login", json={"login": login, "password": password})
    assert r.status_code == 200, r.text
    return c


def _anon(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── Вход / выход / me ─────────────────────────────────────────────────────────


async def test_login_ok_and_me(app, seed):
    c = await _login(app, "alice", "alicepass")
    try:
        r = await c.get("/api/auth/me")
        assert r.status_code == 200
        body = r.json()
        assert body["login"] == "alice"
        assert body["role"] == "client"
        assert body["profile_id"] == seed["a"]
    finally:
        await c.aclose()


async def test_login_wrong_password(app, seed):
    c = _anon(app)
    try:
        r = await c.post("/api/auth/login", json={"login": "alice", "password": "nope"})
        assert r.status_code == 401
    finally:
        await c.aclose()


async def test_login_unknown_user(app, seed):
    c = _anon(app)
    try:
        r = await c.post("/api/auth/login", json={"login": "ghost", "password": "x"})
        assert r.status_code == 401
    finally:
        await c.aclose()


async def test_logout_clears_session(app, seed):
    c = await _login(app, "alice", "alicepass")
    try:
        assert (await c.get("/api/auth/me")).status_code == 200
        await c.post("/api/auth/logout")
        c.cookies.clear()
        assert (await c.get("/api/auth/me")).status_code == 401
    finally:
        await c.aclose()


# ── Требуется вход (401 без куки) ─────────────────────────────────────────────


async def test_anonymous_blocked_everywhere(app, seed):
    c = _anon(app)
    try:
        assert (await c.get("/api/profiles")).status_code == 401
        assert (await c.get(f"/api/profiles/{seed['a']}")).status_code == 401
        assert (await c.get(f"/api/profiles/{seed['a']}/channels")).status_code == 401
    finally:
        await c.aclose()


# ── ИЗОЛЯЦИЯ: client A не видит профиль B ──────────────────────────────────────


async def test_client_cannot_read_foreign_profile(app, seed):
    c = await _login(app, "alice", "alicepass")
    try:
        own = await c.get(f"/api/profiles/{seed['a']}")
        assert own.status_code == 200  # свой — можно
        foreign = await c.get(f"/api/profiles/{seed['b']}")
        assert foreign.status_code == 403  # чужой — 403 на бэкенде
    finally:
        await c.aclose()


async def test_client_cannot_touch_foreign_channels(app, seed):
    """Гейт роутера channels/publish: чужой profile_id → 403, не только в UI."""
    c = await _login(app, "alice", "alicepass")
    try:
        assert (await c.get(f"/api/profiles/{seed['b']}/channels")).status_code == 403
        r = await c.get(f"/api/profiles/{seed['b']}/publish/targets?content_type=post")
        assert r.status_code == 403
    finally:
        await c.aclose()


async def test_client_list_shows_only_own(app, seed):
    c = await _login(app, "alice", "alicepass")
    try:
        r = await c.get("/api/profiles")
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()]
        assert ids == [seed["a"]]  # видит ТОЛЬКО свой
    finally:
        await c.aclose()


async def test_client_cannot_create_profile(app, seed):
    c = await _login(app, "alice", "alicepass")
    try:
        r = await c.post("/api/profiles", json={"name": "sneaky"})
        assert r.status_code == 403  # создание профилей — только admin
    finally:
        await c.aclose()


# ── admin видит всё ────────────────────────────────────────────────────────────


async def test_admin_sees_all_profiles(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        r = await c.get("/api/profiles")
        assert r.status_code == 200
        ids = {p["id"] for p in r.json()}
        assert {seed["a"], seed["b"]} <= ids
        # и в любой может зайти
        assert (await c.get(f"/api/profiles/{seed['b']}")).status_code == 200
        assert (await c.get(f"/api/profiles/{seed['a']}/channels")).status_code == 200
    finally:
        await c.aclose()


# ── admin заводит client-аккаунт ───────────────────────────────────────────────


async def test_admin_creates_client_who_can_login(app, seed):
    admin = await _login(app, "admin", "adminpass")
    try:
        r = await admin.post(
            "/api/users",
            json={"login": "bob", "password": "bobpass", "profile_id": seed["b"]},
        )
        assert r.status_code == 201, r.text
        assert r.json()["role"] == "client"
        assert r.json()["profile_id"] == seed["b"]
    finally:
        await admin.aclose()

    bob = await _login(app, "bob", "bobpass")
    try:
        # bob видит только B, а A ему запрещён
        assert [p["id"] for p in (await bob.get("/api/profiles")).json()] == [seed["b"]]
        assert (await bob.get(f"/api/profiles/{seed['a']}")).status_code == 403
    finally:
        await bob.aclose()


async def test_client_cannot_create_users(app, seed):
    c = await _login(app, "alice", "alicepass")
    try:
        r = await c.post(
            "/api/users", json={"login": "x", "password": "yyyyyy", "profile_id": seed["a"]}
        )
        assert r.status_code == 403  # только admin
        assert (await c.get("/api/users")).status_code == 403
    finally:
        await c.aclose()


async def test_admin_create_client_bad_profile_and_dup(app, seed):
    admin = await _login(app, "admin", "adminpass")
    try:
        # несуществующий профиль
        r = await admin.post(
            "/api/users", json={"login": "carol", "password": "secret", "profile_id": 9999}
        )
        assert r.status_code == 404
        # дублирующийся логин (alice уже есть)
        r = await admin.post(
            "/api/users", json={"login": "alice", "password": "secret", "profile_id": seed["a"]}
        )
        assert r.status_code == 409
    finally:
        await admin.aclose()
