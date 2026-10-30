"""Tests for POST /api/profiles/{pid}/channels/telegram/targets/from-link."""

from __future__ import annotations

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


async def _profile(factory, name="acme") -> int:
    async with factory() as s:
        return (await ProfileRepository(s, vault=get_vault()).create_profile(name)).id


async def _connect_telegram(factory, pid: int) -> None:
    blob = build_credential_blob(api_id=123, api_hash="abc", target_channel="@seed", session="SESS")
    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        await repo.set_credential(pid, "telegram", CredentialKind.API_TOKEN, blob)


def _channel_entity(*, id: int = 1001, title="Chan", access_hash=42):
    from telethon.tl import types as tl

    return tl.Channel(
        id=id,
        title=title,
        photo=None,
        date=None,
        broadcast=True,
        creator=True,
        access_hash=access_hash,
    )


def _forum_entity(*, id: int = 2001, title="Forum", access_hash=99):
    from telethon.tl import types as tl

    ch = tl.Channel(
        id=id,
        title=title,
        photo=None,
        date=None,
        broadcast=False,
        creator=True,
        megagroup=True,
        access_hash=access_hash,
    )
    ch.forum = True
    return ch


def _topic_entity(*, id: int, title: str, closed: bool = False):
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


def _mock_client(entity, topics=None):
    """Build an AsyncMock Telethon client for the from-link endpoint."""
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=entity)
    client.disconnect = AsyncMock()

    if topics is not None:
        resp = MagicMock()
        resp.topics = topics
        # list_forum_topics calls client(GetForumTopicsRequest(...)) in a loop
        # Return one page then raise StopIteration (empty second page)
        empty = MagicMock()
        empty.topics = []
        client.side_effect = [resp, empty]
    else:
        # not a forum — raise when called with GetForumTopicsRequest
        from telethon.errors import ChannelForumMissingError  # type: ignore[import]

        client.side_effect = ChannelForumMissingError(None)

    return client


URL = "/api/profiles/{pid}/channels/telegram/targets/from-link"


# ── happy path: valid topic link ──────────────────────────────────────────────


async def test_from_link_valid_topic(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)
    forum = _forum_entity(id=200111, title="My Forum", access_hash=77)
    topic = _topic_entity(id=42, title="News")
    fake_client = _mock_client(forum, topics=[topic])

    ac, _ = await _admin_client(factory)
    with patch(
        "crosspost.web.routes.telegram_discovery._build_discovery_client",
        new=AsyncMock(return_value=fake_client),
    ):
        async with ac:
            r = await ac.post(
                URL.format(pid=pid),
                json={"url": "https://t.me/c/200111/42/500"},
            )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["name"] == "My Forum → News"

    # Check DB — one created target has topic_id=42
    async with factory() as s:
        trepo = ChannelTargetRepository(s, vault=get_vault(), profile_id=pid)
        targets = await trepo.list_targets("telegram")
    topic_targets = [t for t in targets if t.topic_id == 42]
    assert len(topic_targets) == 1
    assert topic_targets[0].peer_access_hash == 77


# ── chat link without forum topics (regular channel message link) ─────────────


async def test_from_link_channel_no_topic(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)
    ch = _channel_entity(id=3001, title="My Channel", access_hash=55)
    fake_client = _mock_client(ch, topics=None)  # side_effect = ChannelForumMissingError

    ac, _ = await _admin_client(factory)
    with patch(
        "crosspost.web.routes.telegram_discovery._build_discovery_client",
        new=AsyncMock(return_value=fake_client),
    ):
        async with ac:
            r = await ac.post(
                URL.format(pid=pid),
                json={"url": "https://t.me/c/3001/500"},
            )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["name"] == "My Channel"

    async with factory() as s:
        trepo = ChannelTargetRepository(s, vault=get_vault(), profile_id=pid)
        targets = await trepo.list_targets("telegram")
    assert targets[0].topic_id is None


# ── public @username link (no topic — General, parsed as None) ────────────────


async def test_from_link_username_general_topic(factory):
    """t.me/channelname/1 → user_topic=1 → topic_id=None by parse_telegram_link."""
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)
    ch = _channel_entity(id=4001, title="Public Chan", access_hash=11)
    fake_client = AsyncMock()
    fake_client.get_entity = AsyncMock(return_value=ch)
    fake_client.disconnect = AsyncMock()

    ac, _ = await _admin_client(factory)
    with patch(
        "crosspost.web.routes.telegram_discovery._build_discovery_client",
        new=AsyncMock(return_value=fake_client),
    ):
        async with ac:
            r = await ac.post(
                URL.format(pid=pid),
                json={"url": "https://t.me/mychannel/1"},
            )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["name"] == "Public Chan"


# ── invalid link ──────────────────────────────────────────────────────────────


async def test_from_link_invalid_url(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)

    ac, _ = await _admin_client(factory)
    async with ac:
        r = await ac.post(URL.format(pid=pid), json={"url": "https://example.com/foo"})

    assert r.status_code == 422


# ── no access ─────────────────────────────────────────────────────────────────


async def test_from_link_no_access(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)

    fake_client = AsyncMock()
    fake_client.get_entity = AsyncMock(side_effect=Exception("Not found"))
    fake_client.disconnect = AsyncMock()

    ac, _ = await _admin_client(factory)
    with patch(
        "crosspost.web.routes.telegram_discovery._build_discovery_client",
        new=AsyncMock(return_value=fake_client),
    ):
        async with ac:
            r = await ac.post(
                URL.format(pid=pid),
                json={"url": "https://t.me/c/99999/1/1"},
            )

    assert r.status_code == 422
    assert "доступа" in r.json()["detail"].lower()


# ── idempotency ───────────────────────────────────────────────────────────────


async def test_from_link_idempotent(factory):
    pid = await _profile(factory)
    await _connect_telegram(factory, pid)
    ch = _channel_entity(id=5001, title="Idempotent Chan", access_hash=33)

    def _fresh_client():
        c = AsyncMock()
        c.get_entity = AsyncMock(return_value=ch)
        c.disconnect = AsyncMock()
        c.side_effect = Exception("not a forum")
        return c

    ac, _ = await _admin_client(factory)
    with patch(
        "crosspost.web.routes.telegram_discovery._build_discovery_client",
        new=AsyncMock(side_effect=lambda *_: _fresh_client()),
    ):
        async with ac:
            r1 = await ac.post(URL.format(pid=pid), json={"url": "https://t.me/c/5001/500"})
            r2 = await ac.post(URL.format(pid=pid), json={"url": "https://t.me/c/5001/999"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["created"] is True
    assert r2.json()["created"] is False  # same target_value + topic_id=None
    assert r1.json()["target_id"] == r2.json()["target_id"]
