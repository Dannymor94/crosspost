"""Тесты CRUD целей канала (мультицели). Milestone «мультицели», Блок 5.

Проверяем profile-scoped роуты списка целей канала:
  - создать/список/переименовать/сменить значение/удалить;
  - значение НОРМАЛИЗУЕТСЯ сервером (URL группы → screen_name) и в БД шифртекст;
  - мусорное значение → 422;
  - ленивый сид «Основной» из legacy-одиночной цели (CredentialKind.TARGET);
  - канал без целей (telegraph) → 400; несуществующий канал → 404;
  - декларация поля цели (target_field) отдаётся в статусе канала.
Изоляция (client чужого профиля → 403) — в test_auth_api.py.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import CredentialKind
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.vault import get_vault
from crosspost.web import deps
from crosspost.web.routes import channels, profiles


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
async def client(factory) -> AsyncGenerator[AsyncClient, None]:
    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    for r in (profiles, channels):
        app.include_router(r.router)

    from crosspost.auth.sessions import sign_session
    from crosspost.db.models import Role
    from crosspost.db.user_repo import UserRepository

    async with factory() as s:
        admin = await UserRepository(s).create_user("admin", "adminpass", role=Role.ADMIN)
        admin_id = admin.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("session", sign_session(admin_id))
        yield c


async def _profile(factory, name="acme") -> int:
    async with factory() as s:
        return (await ProfileRepository(s, vault=get_vault()).create_profile(name)).id


# ── CRUD ──────────────────────────────────────────────────────────────────────


async def test_create_list_rename_delete_target(client, factory):
    pid = await _profile(factory)
    base = f"/api/profiles/{pid}/channels/vk_wall/targets"

    # создать: сырой URL нормализуется в screen_name
    created = await client.post(
        base, json={"name": "Группа А", "value": "https://vk.com/yogapiecom"}
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["name"] == "Группа А"
    assert body["value"] == "yogapiecom"  # нормализовано сервером
    tid = body["id"]

    # список отдаёт созданную цель
    listed = await client.get(base)
    assert [t["id"] for t in listed.json()] == [tid]
    assert listed.json()[0]["value"] == "yogapiecom"

    # переименование
    renamed = await client.patch(f"{base}/{tid}", json={"name": "Группа Б"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Группа Б"
    assert renamed.json()["value"] == "yogapiecom"  # значение не тронуто

    # смена значения (тоже нормализуется)
    revalued = await client.patch(f"{base}/{tid}", json={"value": "vk.com/newgroup"})
    assert revalued.status_code == 200, revalued.text
    assert revalued.json()["value"] == "newgroup"

    # удаление
    deleted = await client.delete(f"{base}/{tid}")
    assert deleted.status_code == 200
    assert (await client.get(base)).json() == []


async def test_multiple_targets_on_one_channel(client, factory):
    pid = await _profile(factory)
    base = f"/api/profiles/{pid}/channels/vk_wall/targets"
    await client.post(base, json={"name": "Первая", "value": "groupone"})
    await client.post(base, json={"name": "Вторая", "value": "grouptwo"})
    values = {t["name"]: t["value"] for t in (await client.get(base)).json()}
    assert values == {"Первая": "groupone", "Вторая": "grouptwo"}


async def test_create_target_garbage_value_422(client, factory):
    pid = await _profile(factory)
    base = f"/api/profiles/{pid}/channels/vk_wall/targets"
    resp = await client.post(base, json={"name": "Кривая", "value": "не адрес!!!"})
    assert resp.status_code == 422


async def test_create_target_empty_name_422(client, factory):
    pid = await _profile(factory)
    base = f"/api/profiles/{pid}/channels/vk_wall/targets"
    resp = await client.post(base, json={"name": "  ", "value": "groupone"})
    assert resp.status_code == 422


async def test_targetless_channel_400(client, factory):
    """telegraph не имеет целей публикации → 400 на CRUD целей."""
    pid = await _profile(factory)
    resp = await client.get(f"/api/profiles/{pid}/channels/telegraph/targets")
    assert resp.status_code == 400


async def test_unknown_channel_404(client, factory):
    pid = await _profile(factory)
    resp = await client.get(f"/api/profiles/{pid}/channels/nosuch/targets")
    assert resp.status_code == 404


async def test_patch_missing_target_404(client, factory):
    pid = await _profile(factory)
    resp = await client.patch(
        f"/api/profiles/{pid}/channels/vk_wall/targets/99999", json={"name": "x"}
    )
    assert resp.status_code == 404


# ── Ленивый сид из legacy-одиночной цели ──────────────────────────────────────


async def test_lazy_seed_from_legacy_single_target(client, factory):
    """Была одиночная цель (CredentialKind.TARGET) → первый list отдаёт «Основную»."""
    pid = await _profile(factory)
    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        await repo.set_credential(pid, "vk_wall", CredentialKind.TARGET, "legacygroup")

    base = f"/api/profiles/{pid}/channels/vk_wall/targets"
    listed = (await client.get(base)).json()
    assert len(listed) == 1
    assert listed[0]["name"] == "Основная"
    assert listed[0]["value"] == "legacygroup"

    # повторный list НЕ дублирует сид
    assert len((await client.get(base)).json()) == 1


# ── Значение шифруется в БД ───────────────────────────────────────────────────


async def test_target_value_encrypted_at_rest(client, factory):
    pid = await _profile(factory)
    base = f"/api/profiles/{pid}/channels/vk_wall/targets"
    await client.post(base, json={"name": "Секрет", "value": "yogapiecom"})

    async with factory() as s:
        rows = (await s.execute(text("SELECT value_blob FROM channel_targets"))).all()
    assert rows, "цель должна быть в БД"
    for (blob,) in rows:
        assert "yogapiecom" not in blob  # в БД — шифртекст, не открытое значение


# ── Декларация поля цели в статусе канала ─────────────────────────────────────


async def test_channel_status_exposes_target_field(client, factory):
    """Статус канала несёт декларацию поля цели: vk_wall — есть, telegraph — нет."""
    pid = await _profile(factory)
    statuses = {c["channel"]: c for c in (await client.get(f"/api/profiles/{pid}/channels")).json()}

    vk = statuses["vk_wall"]
    assert vk["target_field"] is not None
    assert vk["target_field"]["name"] == "target"

    assert statuses["telegraph"]["target_field"] is None
