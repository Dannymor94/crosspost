"""Тесты админ-консоли ошибок (Трек C): /api/admin/logs*.

Ключевое:
  • admin видит логи ВСЕХ профилей; client → 403 (изоляция на бэкенде, не в UI);
  • по умолчанию только проблемы (WARNING/ERROR), переключатель «все» отдаёт INFO;
  • фильтры: клиент, канал, уровень, период (from_time);
  • пагинация по курсору id (before / since);
  • сводка: клиентов с проблемами + разбивка по каналам;
  • скриншот сбоя — лениво, отдельным эндпоинтом; 404 если нет файла.
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
from crosspost.db.log_repo import LogRepository
from crosspost.db.models import Role
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.user_repo import UserRepository
from crosspost.db.vault import get_vault
from crosspost.web import deps
from crosspost.web.routes import auth, errors

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "test-secret-please-change")


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
    a.include_router(errors.router)
    return a


@pytest_asyncio.fixture
async def seed(factory) -> dict:
    """Профили A, B + логи; admin и client alice→A."""
    async with factory() as s:
        prepo = ProfileRepository(s, vault=get_vault())
        a = await prepo.create_profile("client-A")
        b = await prepo.create_profile("client-B")
        urepo = UserRepository(s)
        await urepo.create_user("admin", "adminpass", role=Role.ADMIN)
        await urepo.create_user("alice", "alicepass", role=Role.CLIENT, profile_id=a.id)
        log = LogRepository(s)
        await log.add(a.id, channel="telegram", level="INFO", message="ok")
        await log.add(a.id, channel="telegram", level="ERROR", message="tg failed")
        await log.add(
            a.id,
            channel="vk_wall",
            level="WARNING",
            message="vk slow",
            screenshot_path="/nonexistent/shot.png",
        )
        await log.add(b.id, channel="telegram", level="ERROR", message="tg B failed")
        await log.add(b.id, channel="yandex", level="ERROR", message="ya failed")
    return {"a": a.id, "b": b.id}


async def _login(app: FastAPI, login: str, password: str) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    r = await c.post("/api/auth/login", json={"login": login, "password": password})
    assert r.status_code == 200, r.text
    return c


# ── Доступ ────────────────────────────────────────────────────────────────────


async def test_client_forbidden(app, seed):
    c = await _login(app, "alice", "alicepass")
    try:
        for path in ("/api/admin/logs", "/api/admin/logs/summary"):
            r = await c.get(path)
            assert r.status_code == 403, path
    finally:
        await c.aclose()


async def test_anonymous_unauthorized(app, seed):
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        r = await c.get("/api/admin/logs")
        assert r.status_code == 401
    finally:
        await c.aclose()


# ── Лента ─────────────────────────────────────────────────────────────────────


async def test_admin_sees_all_profiles_problems_only(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        r = await c.get("/api/admin/logs")
        assert r.status_code == 200
        rows = r.json()
        # INFO отфильтрован → 4 проблемы из 5 логов
        assert len(rows) == 4
        assert {row["profile_name"] for row in rows} == {"client-A", "client-B"}
        assert all(row["level"] in ("WARNING", "ERROR") for row in rows)
    finally:
        await c.aclose()


async def test_show_all_includes_info(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        r = await c.get("/api/admin/logs", params={"only_problems": "false"})
        rows = r.json()
        assert len(rows) == 5
        assert any(row["level"] == "INFO" for row in rows)
    finally:
        await c.aclose()


async def test_filter_by_client_and_channel(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        r = await c.get("/api/admin/logs", params={"profile_id": seed["b"]})
        assert {row["profile_id"] for row in r.json()} == {seed["b"]}
        r2 = await c.get("/api/admin/logs", params={"channel": "telegram"})
        assert all(row["channel"] == "telegram" for row in r2.json())
        assert len(r2.json()) == 2
    finally:
        await c.aclose()


async def test_filter_by_level(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        r = await c.get("/api/admin/logs", params={"level": "warning"})
        rows = r.json()
        assert all(row["level"] == "WARNING" for row in rows)
        assert len(rows) == 1
        bad = await c.get("/api/admin/logs", params={"level": "bogus"})
        assert bad.status_code == 422
    finally:
        await c.aclose()


async def test_period_filter_future_empty(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        r = await c.get("/api/admin/logs", params={"from_time": "2999-01-01T00:00:00"})
        assert r.json() == []
        bad = await c.get("/api/admin/logs", params={"from_time": "not-a-date"})
        assert bad.status_code == 422
    finally:
        await c.aclose()


async def test_pagination_before_and_since(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        page1 = (await c.get("/api/admin/logs", params={"limit": 2})).json()
        assert len(page1) == 2
        oldest = page1[-1]["cursor"]
        page2 = (await c.get("/api/admin/logs", params={"limit": 2, "before": oldest})).json()
        assert all(row["cursor"] < oldest for row in page2)
        newest = page1[0]["cursor"]
        delta = (await c.get("/api/admin/logs", params={"since": newest})).json()
        assert delta == []
    finally:
        await c.aclose()


# ── Сводка ────────────────────────────────────────────────────────────────────


async def test_summary(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        r = await c.get("/api/admin/logs/summary")
        data = r.json()
        assert data["clients_with_problems"] == 2
        chan = {c["channel"]: c["count"] for c in data["channels"]}
        assert chan["telegram"] == 2
        assert chan["yandex"] == 1
        assert data["channels"][0]["channel"] == "telegram"  # чаще всех
    finally:
        await c.aclose()


# ── Скриншот (лениво) ─────────────────────────────────────────────────────────


async def test_screenshot_flag_in_feed(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        rows = (await c.get("/api/admin/logs")).json()
        with_shot = [row for row in rows if row["has_screenshot"]]
        assert len(with_shot) == 1
        assert with_shot[0]["channel"] == "vk_wall"
    finally:
        await c.aclose()


async def test_screenshot_missing_file_404(app, seed):
    c = await _login(app, "admin", "adminpass")
    try:
        rows = (await c.get("/api/admin/logs")).json()
        log_id = next(row["id"] for row in rows if row["has_screenshot"])
        # путь ведёт на несуществующий файл → 404 (не 500)
        r = await c.get(f"/api/admin/logs/{log_id}/screenshot")
        assert r.status_code == 404
        # запись без скриншота → тоже 404
        no_shot = next(row["id"] for row in rows if not row["has_screenshot"])
        assert (await c.get(f"/api/admin/logs/{no_shot}/screenshot")).status_code == 404
    finally:
        await c.aclose()


async def test_screenshot_client_forbidden(app, seed):
    c = await _login(app, "alice", "alicepass")
    try:
        r = await c.get("/api/admin/logs/1/screenshot")
        assert r.status_code == 403
    finally:
        await c.aclose()
