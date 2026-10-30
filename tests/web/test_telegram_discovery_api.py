"""Tests for Telegram discovery + bulk-create API endpoints."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.channels.telegram_login import build_credential_blob
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import CredentialKind, Role
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.target_repo import ChannelTargetRepository
from crosspost.db.user_repo import UserRepository
from crosspost.db.vault import get_vault
from crosspost.web import deps
from crosspost.web.routes import channels, profiles, telegram_discovery

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    telegram_discovery._DIALOG_CACHE.clear()
    telegram_discovery._TOPIC_CACHE.clear()


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(f)
    yield f
    await engine.dispose()


def _app():
    @asynccontextmanager
    async def _lifespan(app):
        # clear discovery cache between tests
        telegram_discovery._DIALOG_CACHE.clear()
        telegram_discovery._TOPIC_CACHE.clear()
        yield

    app = FastAPI(lifespan=_lifespan)
    for r in (profiles, channels, telegram_discovery):
        app.include_router(r.router)
    return app


async def _admin_client(factory) -> tuple[AsyncClient, int]:
    async with factory() as s:
        admin = await UserRepository(s).create_user("admin", "pw", role=Role.ADMIN)
        admin_id = admin.id
    from crosspost.auth.sessions import sign_session

    client = AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test")
    client.cookies.set("session", sign_session(admin_id))
    return client, admin_id


async def _client_user(factory, profile_id: int) -> AsyncClient:
    async with factory() as s:
        u = await UserRepository(s).create_user("cli", "pw", role=Role.CLIENT)
        # attach to profile
        from sqlalchemy import update

        from crosspost.db.models import User

        await s.execute(update(User).where(User.id == u.id).values(profile_id=profile_id))
        await s.commit()
        uid = u.id
    from crosspost.auth.sessions import sign_session

    client = AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test")
    client.cookies.set("session", sign_session(uid))
    return client


async def _profile(factory, name="acme") -> int:
    async with factory() as s:
        return (await ProfileRepository(s, vault=get_vault()).create_profile(name)).id


async def _connect_telegram(factory, pid: int) -> None:
    blob = build_credential_blob(api_id=123, api_hash="abc", target_channel="@seed", session="SESS")
    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        await repo.set_credential(pid, "telegram", CredentialKind.API_TOKEN, blob)


def _fake_dialog(entity):
    d = MagicMock()
    d.entity = entity
    return d


def _channel_entity(*, id: int = 1001, title="Chan", broadcast=True, creator=True, access_hash=42):
    from telethon.tl import types as tl

    return tl.Channel(
        id=id,
        title=title,
        photo=None,
        date=None,
        broadcast=broadcast,
        creator=creator,
        access_hash=access_hash,
    )


def _topic_entity(*, id: int = 5, title="Topic", closed=False):
    from telethon.tl import types as tl

    return tl.ForumTopic(
        id=id,
        date=None,
        peer=None,
        title=title,
        icon_color=0,
        top_message=id * 10,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=None,
        notify_settings=None,
        closed=closed,
    )


def _mock_telethon(dialogs=None, topic_pages=None):
    """Patch _build_discovery_client to return a controlled AsyncMock client."""
    client = AsyncMock()
    client.get_dialogs = AsyncMock(return_value=[_fake_dialog(e) for e in (dialogs or [])])

    if topic_pages is not None:
        pages = topic_pages
        resp_list = []
        for page in pages:
            r = MagicMock()
            r.topics = page
            resp_list.append(r)
        client.side_effect = resp_list

    return client


# ── /dialogs ──────────────────────────────────────────────────────────────────


async def test_dialogs_requires_auth(factory):
    pid = await _profile(factory)
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/api/profiles/{pid}/channels/telegram/dialogs")
    assert r.status_code == 401


async def test_dialogs_forbidden_for_client(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)
    client = await _client_user(factory, pid)
    async with client:
        r = await client.get(f"/api/profiles/{pid}/channels/telegram/dialogs")
    assert r.status_code == 403


async def test_dialogs_no_credentials_returns_404(factory):
    pid = await _profile(factory)
    ac, _ = await _admin_client(factory)
    async with ac:
        r = await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs")
    assert r.status_code == 404


async def test_dialogs_returns_items(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)

    entity = _channel_entity(id=1001, title="My Chan", access_hash=99)
    fake_client = _mock_telethon(dialogs=[entity])

    ac, _ = await _admin_client(factory)
    with patch(
        "crosspost.web.routes.telegram_discovery._build_discovery_client",
        new=AsyncMock(return_value=fake_client),
    ):
        async with ac:
            r = await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs")

    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "fetched_at" in body
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["target_value"] == "-1001001"
    assert item["access_hash"] == 99
    assert item["title"] == "My Chan"
    assert item["kind"] == "channel"


async def test_dialogs_cache_hit(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)

    entity = _channel_entity()
    fake_client = _mock_telethon(dialogs=[entity])

    ac, _ = await _admin_client(factory)
    mock_build = AsyncMock(return_value=fake_client)
    with patch("crosspost.web.routes.telegram_discovery._build_discovery_client", new=mock_build):
        async with ac:
            await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs")
            r2 = await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs")
        # client built only once (second hit from cache)
        assert mock_build.call_count == 1
    assert r2.status_code == 200


async def test_dialogs_refresh_bypasses_cache(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)

    fake_client = _mock_telethon(dialogs=[])

    ac, _ = await _admin_client(factory)
    mock_build = AsyncMock(return_value=fake_client)
    with patch("crosspost.web.routes.telegram_discovery._build_discovery_client", new=mock_build):
        async with ac:
            await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs")
            await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs?refresh=1")
        assert mock_build.call_count == 2


async def test_dialogs_flood_wait_returns_429(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)

    from telethon.errors import FloodWaitError as FWE

    fake_client = AsyncMock()

    class _FakeReq:
        pass

    flood_error = FWE(request=_FakeReq())
    flood_error.seconds = 30
    fake_client.get_dialogs = AsyncMock(side_effect=flood_error)

    ac, _ = await _admin_client(factory)
    with patch(
        "crosspost.web.routes.telegram_discovery._build_discovery_client",
        new=AsyncMock(return_value=fake_client),
    ):
        async with ac:
            r = await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs")

    assert r.status_code == 429
    body = r.json()["detail"]
    assert body["retry_after"] == 30
    assert body["items"] == []  # nothing cached yet


async def test_dialogs_flood_wait_returns_cached_items(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)

    entity = _channel_entity()

    from telethon.errors import FloodWaitError as FWE

    class _FakeReq:
        pass

    flood = FWE(request=_FakeReq())
    flood.seconds = 60

    good_client = _mock_telethon(dialogs=[entity])
    bad_client = AsyncMock()
    bad_client.get_dialogs = AsyncMock(side_effect=flood)

    ac, _ = await _admin_client(factory)
    with patch(
        "crosspost.web.routes.telegram_discovery._build_discovery_client",
        new=AsyncMock(side_effect=[good_client, bad_client]),
    ):
        async with ac:
            await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs")
            r = await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs?refresh=1")

    assert r.status_code == 429
    body = r.json()["detail"]
    assert len(body["items"]) == 1  # stale cache returned


# ── /dialogs/{target_value}/topics ────────────────────────────────────────────


async def test_topics_requires_admin(factory):
    pid = await _profile(factory)
    client = await _client_user(factory, pid)
    async with client:
        r = await client.get(f"/api/profiles/{pid}/channels/telegram/dialogs/-1001001/topics")
    assert r.status_code == 403


async def test_topics_returns_items(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)

    general = _topic_entity(id=1, title="General")
    topic5 = _topic_entity(id=5, title="Intro")

    fake_client = AsyncMock()
    resp = MagicMock()
    resp.topics = [general, topic5]
    fake_client.side_effect = [resp, MagicMock(topics=[])]

    ac, _ = await _admin_client(factory)
    with patch(
        "crosspost.web.routes.telegram_discovery._build_discovery_client",
        new=AsyncMock(return_value=fake_client),
    ):
        async with ac:
            r = await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs/-1001001/topics")

    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    general_item = next(i for i in body["items"] if i["title"] == "General")
    assert general_item["topic_id"] is None
    other = next(i for i in body["items"] if i["title"] == "Intro")
    assert other["topic_id"] == 5


async def test_topics_flood_wait_429_with_cached(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)

    from telethon.errors import FloodWaitError as FWE

    class _FakeReq:
        pass

    topic = _topic_entity(id=5, title="T")
    good_resp = MagicMock()
    good_resp.topics = [topic]
    good_resp2 = MagicMock()
    good_resp2.topics = []

    flood = FWE(request=_FakeReq())
    flood.seconds = 20

    good_client = AsyncMock(side_effect=[good_resp, good_resp2])
    bad_client = AsyncMock(side_effect=flood)

    tv = "-1001001"
    ac, _ = await _admin_client(factory)
    with patch(
        "crosspost.web.routes.telegram_discovery._build_discovery_client",
        new=AsyncMock(side_effect=[good_client, bad_client]),
    ):
        async with ac:
            await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs/{tv}/topics")
            r = await ac.get(f"/api/profiles/{pid}/channels/telegram/dialogs/{tv}/topics?refresh=1")

    assert r.status_code == 429
    assert r.json()["detail"]["retry_after"] == 20
    assert len(r.json()["detail"]["items"]) == 1


# ── /targets/bulk ─────────────────────────────────────────────────────────────


async def test_bulk_requires_admin(factory):
    pid = await _profile(factory)
    client = await _client_user(factory, pid)
    async with client:
        r = await client.post(
            f"/api/profiles/{pid}/channels/telegram/targets/bulk",
            json=[{"target_value": "@x"}],
        )
    assert r.status_code == 403


async def test_bulk_creates_targets(factory):
    pid = await _profile(factory)
    ac, _ = await _admin_client(factory)
    async with ac:
        r = await ac.post(
            f"/api/profiles/{pid}/channels/telegram/targets/bulk",
            json=[
                {
                    "target_value": "-1001001",
                    "access_hash": 42,
                    "topic_id": 5,
                    "name": "Forum topic",
                },
                {"target_value": "@plain", "name": "Plain"},
            ],
        )
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    assert all(i["created"] is True for i in items)
    assert {i["name"] for i in items} == {"Forum topic", "Plain"}


async def test_bulk_topic_1_stored_as_null(factory):
    pid = await _profile(factory)
    ac, _ = await _admin_client(factory)
    async with ac:
        r = await ac.post(
            f"/api/profiles/{pid}/channels/telegram/targets/bulk",
            json=[{"target_value": "@g", "topic_id": 1, "name": "General"}],
        )
        assert r.status_code == 200
        # verify via list
        r2 = await ac.get(f"/api/profiles/{pid}/channels/telegram/targets")
    t = next(t for t in r2.json() if t["value"] == "@g")
    assert t["topic_id"] is None


async def test_bulk_idempotent_returns_existing(factory):
    pid = await _profile(factory)
    ac, _ = await _admin_client(factory)
    async with ac:
        r1 = await ac.post(
            f"/api/profiles/{pid}/channels/telegram/targets/bulk",
            json=[{"target_value": "@ch", "name": "Chan"}],
        )
        r2 = await ac.post(
            f"/api/profiles/{pid}/channels/telegram/targets/bulk",
            json=[{"target_value": "@ch", "name": "Chan2"}],
        )
    assert r1.json()[0]["target_id"] == r2.json()[0]["target_id"]
    assert r2.json()[0]["created"] is False


async def test_bulk_backfills_access_hash(factory):
    pid = await _profile(factory)
    async with factory() as s:
        trepo = ChannelTargetRepository(s, vault=get_vault(), profile_id=pid)
        t = await trepo.add_target("telegram", "Chan", "-1001001")
        tid = t.id

    ac, _ = await _admin_client(factory)
    async with ac:
        r = await ac.post(
            f"/api/profiles/{pid}/channels/telegram/targets/bulk",
            json=[{"target_value": "-1001001", "access_hash": 777}],
        )
    assert r.json()[0]["target_id"] == tid
    assert r.json()[0]["created"] is False
    # verify hash is set
    async with factory() as s:
        trepo = ChannelTargetRepository(s, vault=get_vault(), profile_id=pid)
        updated = await trepo.get_target(tid)
    assert updated.peer_access_hash == 777


async def test_bulk_default_name_from_cache(factory):
    """Name falls back to target_value when no cache; uses chat title when cached."""
    pid = await _profile(factory)
    # pre-populate dialog cache
    telegram_discovery._DIALOG_CACHE[pid] = (
        time.time(),
        [
            {
                "target_value": "-1001999",
                "title": "Йога",
                "kind": "forum",
                "access_hash": 1,
                "username": None,
                "can_post": True,
                "reason": None,
            }
        ],
    )
    ac, _ = await _admin_client(factory)
    async with ac:
        r = await ac.post(
            f"/api/profiles/{pid}/channels/telegram/targets/bulk",
            json=[{"target_value": "-1001999"}],  # no name
        )
    name = r.json()[0]["name"]
    assert "Йога" in name


async def test_bulk_default_name_fallback_to_target_value(factory):
    """No cache → name = target_value."""
    pid = await _profile(factory)
    telegram_discovery._DIALOG_CACHE.clear()
    ac, _ = await _admin_client(factory)
    async with ac:
        r = await ac.post(
            f"/api/profiles/{pid}/channels/telegram/targets/bulk",
            json=[{"target_value": "@unknown"}],
        )
    assert r.json()[0]["name"] == "@unknown"


async def test_bulk_general_topic_name_is_chat_title_only(factory):
    """topic_id=1 (General) → name is just the chat title, no arrow."""
    pid = await _profile(factory)
    telegram_discovery._DIALOG_CACHE[pid] = (
        time.time(),
        [
            {
                "target_value": "-1001999",
                "title": "Форум",
                "kind": "forum",
                "access_hash": 1,
                "username": None,
                "can_post": True,
                "reason": None,
            }
        ],
    )
    telegram_discovery._TOPIC_CACHE[(pid, "-1001999")] = (
        time.time(),
        [
            {
                "topic_id": None,
                "title": "General",
                "icon_emoji": None,
                "closed": False,
                "can_post": True,
                "reason": None,
            }
        ],
    )
    ac, _ = await _admin_client(factory)
    async with ac:
        r = await ac.post(
            f"/api/profiles/{pid}/channels/telegram/targets/bulk",
            json=[{"target_value": "-1001999", "topic_id": 1}],
        )
    name = r.json()[0]["name"]
    assert name == "Форум"
    assert "→" not in name
