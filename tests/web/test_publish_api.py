"""Тесты роутов публикации/планирования. Итерация 2а.

Адаптеры подменяются через build_profile_adapter (моки площадок).
Проверяем: targets (live+capability), publish мульти-канал, частичный успех +
ретрай одного, отсечение нерабочего канала, scheduled save/list/cancel, изоляция.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.adapters.base import ChannelResult, ResultStatus
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ConnectionState
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.vault import get_vault
from crosspost.web import deps
from crosspost.web.routes import channels, profiles, publish, tg_login


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(f)
    yield f
    await engine.dispose()
    publish._RUNS.clear()


@pytest_asyncio.fixture
async def client(factory) -> AsyncGenerator[AsyncClient, None]:
    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    for r in (profiles, channels, tg_login, publish):
        app.include_router(r.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _make_profile_with_live(factory, channels_live: list[str]) -> int:
    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile("client-a")
        for ch in channels_live:
            await repo.upsert_connection(p.id, ch, ConnectionState.LIVE)
        return p.id


def _post_files(names=("photo.jpg",)):
    return [("media", (n, b"\xff\xd8\xffbytes", "image/jpeg")) for n in names]


class _MockAdapter:
    def __init__(self, channel, *, result=None, raises=None):
        self.channel = channel
        self._result = result
        self._raises = raises

    async def publish(self, content, *, publication_id):
        if self._raises:
            raise self._raises
        return self._result or ChannelResult(self.channel, ResultStatus.DONE, external_id="ok")


def _patch_adapters(mapping):
    async def fake_build(repo, profile_id, channel, *, store=None):
        return mapping.get(channel)

    return patch("crosspost.web.routes.publish.build_profile_adapter", new=fake_build)


# ── Targets ───────────────────────────────────────────────────────────────────


async def test_targets_reflect_live_and_capability(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"])  # vk_wall НЕ live

    resp = await client.get(f"/api/profiles/{pid}/publish/targets?content_type=post")
    assert resp.status_code == 200
    by = {t["channel"]: t for t in resp.json()}

    assert by["telegram"]["eligible"] is True
    assert by["vk_wall"]["eligible"] is False  # не подключён
    assert "подключ" in by["vk_wall"]["reason"].lower()


# ── Publish now ───────────────────────────────────────────────────────────────


async def test_publish_multichannel_done(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram", "vk_wall"])
    adapters = {
        "telegram": _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1")),
        "vk_wall": _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vk1")),
    }
    with _patch_adapters(adapters):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={"content_type": "post", "text": "hi", "channels": json.dumps(["telegram", "vk_wall"])},
            files=_post_files(),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {o["channel"]: o["status"] for o in body["outcomes"]} == {
        "telegram": "done", "vk_wall": "done"
    }

    # Поллинг статуса возвращает то же.
    st = await client.get(f"/api/profiles/{pid}/publish/{body['publication_id']}")
    assert {o["channel"]: o["status"] for o in st.json()} == {"telegram": "done", "vk_wall": "done"}


async def test_partial_success_then_retry_one(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram", "vk_wall"])
    failing = {
        "telegram": _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1")),
        "vk_wall": _MockAdapter("vk_wall", raises=RuntimeError("VK упал")),
    }
    with _patch_adapters(failing):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={"content_type": "post", "text": "hi", "channels": json.dumps(["telegram", "vk_wall"])},
            files=_post_files(),
        )
    body = resp.json()
    pub_id = body["publication_id"]
    by = {o["channel"]: o["status"] for o in body["outcomes"]}
    assert by == {"telegram": "done", "vk_wall": "failed"}

    # Ретраим ТОЛЬКО vk_wall — теперь успешно.
    fixed = {"vk_wall": _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vk9"))}
    with _patch_adapters(fixed):
        r = await client.post(f"/api/profiles/{pid}/publish/{pub_id}/retry/vk_wall")
    assert r.status_code == 200
    assert r.json()["status"] == "done"

    st = {o["channel"]: o["status"] for o in (await client.get(f"/api/profiles/{pid}/publish/{pub_id}")).json()}
    assert st == {"telegram": "done", "vk_wall": "done"}


async def test_publish_rejects_non_live_channel(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"])  # vk_wall не live
    with _patch_adapters({}):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={"content_type": "post", "text": "hi", "channels": json.dumps(["vk_wall"])},
            files=_post_files(),
        )
    assert resp.status_code == 422
    assert "подключ" in resp.json()["detail"].lower()


async def test_publish_requires_media_for_post(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"])
    with _patch_adapters({"telegram": _MockAdapter("telegram")}):
        resp = await client.post(
            f"/api/profiles/{pid}/publish",
            data={"content_type": "post", "text": "hi", "channels": json.dumps(["telegram"])},
        )  # без media
    assert resp.status_code == 422


# ── Scheduled ─────────────────────────────────────────────────────────────────


async def test_schedule_list_cancel(client, factory):
    pid = await _make_profile_with_live(factory, ["telegram"])
    resp = await client.post(
        f"/api/profiles/{pid}/scheduled",
        data={
            "content_type": "post", "text": "later", "channels": json.dumps(["telegram"]),
            "scheduled_at": "2026-08-01T12:00:00+00:00",
        },
        files=_post_files(),
    )
    assert resp.status_code == 200, resp.text
    sched_id = resp.json()["id"]
    assert resp.json()["channels"] == ["telegram"]

    listed = await client.get(f"/api/profiles/{pid}/scheduled")
    assert len(listed.json()) == 1

    dele = await client.delete(f"/api/profiles/{pid}/scheduled/{sched_id}")
    assert dele.status_code == 200
    assert (await client.get(f"/api/profiles/{pid}/scheduled")).json() == []


# ── Изоляция ──────────────────────────────────────────────────────────────────


async def test_publish_isolation_between_profiles(client, factory):
    pa = await _make_profile_with_live(factory, ["telegram"])
    async with factory() as s:
        pb = (await ProfileRepository(s, vault=get_vault()).create_profile("b")).id

    with _patch_adapters({"telegram": _MockAdapter("telegram")}):
        resp = await client.post(
            f"/api/profiles/{pa}/publish",
            data={"content_type": "post", "text": "hi", "channels": json.dumps(["telegram"])},
            files=_post_files(),
        )
    pub_id = resp.json()["publication_id"]

    # Профиль B не видит статусы публикации A.
    other = await client.get(f"/api/profiles/{pb}/publish/{pub_id}")
    assert other.json() == []
