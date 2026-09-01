"""Tests for POST /api/profiles/{profile_id}/publish/preview-blocks.

Covers:
1. text/url/text/url → alternating p/img blocks in order, identical to parse_blocks
2. Inline URL stays inside its paragraph
3. SSRF-blocked host → block error, not dropped
4. Profile isolation (cross-profile access → 403)
5. Endpoint output matches parse_blocks (drift guard)
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.vault import get_vault
from crosspost.web import deps
from crosspost.web.routes import channels, profiles, publish, tg_login

# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "test-secret-please-change")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f = __import__("sqlalchemy.ext.asyncio", fromlist=["async_sessionmaker"]).async_sessionmaker(
        engine, expire_on_commit=False
    )
    deps.set_session_factory(f)
    yield f
    await engine.dispose()
    publish._RUNS.clear()
    # Clear preview cache between tests.
    publish._preview_url_cache.clear()


@pytest_asyncio.fixture
async def client(factory) -> AsyncGenerator[AsyncClient, None]:
    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    for r in (profiles, channels, tg_login, publish):
        app.include_router(r.router)

    from crosspost.auth.sessions import sign_session
    from crosspost.db.models import Role
    from crosspost.db.user_repo import UserRepository

    async with factory() as s:
        admin = await UserRepository(s).create_user("admin", "pw", role=Role.ADMIN)
        admin_id = admin.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("session", sign_session(admin_id))
        yield c


@pytest_asyncio.fixture
async def profile_id(factory) -> int:
    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile("preview-client")
        return p.id


_URL = "/api/profiles/{pid}/publish/preview-blocks"


def _post(client, pid, text):
    return client.post(_URL.format(pid=pid), data={"text": text})


# ── 1. alternating p/img blocks in order ─────────────────────────────────────


async def test_alternating_blocks_in_order(client, profile_id, monkeypatch):
    """text / url / text / url → p img p img, order preserved, same as parse_blocks."""
    image_bytes = b"\x89PNG\r\nFAKE"

    def _resolve_public(host):
        return ["93.184.216.34"]

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=image_bytes))
    mock_client = httpx.AsyncClient(transport=transport)

    monkeypatch.setattr("crosspost.scheduler.media_sources._default_resolve", _resolve_public)

    with patch("crosspost.web.routes.publish._make_preview_client", return_value=mock_client):
        body = "Первый абзац.\n\nhttps://example.com/img1.jpg\n\nВторой абзац.\n\nhttps://example.com/img2.jpg"
        resp = await _post(client, profile_id, body)

    assert resp.status_code == 200
    blocks = resp.json()
    assert len(blocks) == 4
    assert blocks[0]["type"] == "p"
    assert blocks[1]["type"] == "img"
    assert blocks[2]["type"] == "p"
    assert blocks[3]["type"] == "img"
    assert blocks[0]["index"] == 0
    assert blocks[1]["index"] == 1
    assert blocks[1]["url"] == "https://example.com/img1.jpg"


# ── 2. inline URL stays paragraph ────────────────────────────────────────────


async def test_inline_url_stays_paragraph(client, profile_id):
    """URL mixed with other text stays a paragraph block."""
    body = "Смотрите https://example.com/img.jpg для деталей"
    resp = await _post(client, profile_id, body)
    assert resp.status_code == 200
    blocks = resp.json()
    assert len(blocks) == 1
    assert blocks[0]["type"] == "p"
    assert "https://example.com/img.jpg" in blocks[0]["text"]


# ── 3. SSRF-blocked host → error block, not dropped ──────────────────────────


async def test_ssrf_blocked_reported_per_block(client, profile_id, monkeypatch):
    """Private-IP URL → img block with error reason; block index preserved."""
    monkeypatch.setattr(
        "crosspost.scheduler.media_sources._default_resolve",
        lambda host: ["10.0.0.1"],
    )

    body = "Текст.\n\nhttp://internal.host/secret"
    resp = await _post(client, profile_id, body)
    assert resp.status_code == 200
    blocks = resp.json()
    assert len(blocks) == 2
    assert blocks[0]["type"] == "p"
    img = blocks[1]
    assert img["type"] == "img"
    assert img["index"] == 1
    assert img["error"] is not None
    assert img.get("data_uri") is None


# ── 4. profile isolation ──────────────────────────────────────────────────────


async def test_profile_isolation(factory):
    """Client authenticated for profile A cannot preview under profile B."""
    from crosspost.auth.sessions import sign_session
    from crosspost.db.models import Role
    from crosspost.db.user_repo import UserRepository

    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p_a = await repo.create_profile("client-a")
        p_b = await repo.create_profile("client-b")
        ur = UserRepository(s)
        u_a = await ur.create_user("user-a", "pw", role=Role.CLIENT, profile_id=p_a.id)

    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    for r in (profiles, channels, tg_login, publish):
        app.include_router(r.router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("session", sign_session(u_a.id))
        # profile A is accessible
        r_ok = await c.post(_URL.format(pid=p_a.id), data={"text": "hello"})
        assert r_ok.status_code == 200
        # profile B must be forbidden
        r_bad = await c.post(_URL.format(pid=p_b.id), data={"text": "hello"})
        assert r_bad.status_code == 403


# ── 5. drift guard: endpoint agrees with parse_blocks ─────────────────────────


async def test_endpoint_agrees_with_parse_blocks(client, profile_id, monkeypatch):
    """Endpoint block types/urls match parse_blocks output on same input."""
    from crosspost.content.article_blocks import ParagraphBlock, parse_blocks

    monkeypatch.setattr(
        "crosspost.scheduler.media_sources._default_resolve",
        lambda host: ["10.0.0.1"],  # all images fail SSRF — we only check structure
    )

    body = (
        "Введение.\n\n"
        "https://example.com/a.jpg\n\n"
        "Середина текста.\n\n"
        "https://example.com/b.png\n\n"
        "Конец."
    )
    resp = await _post(client, profile_id, body)
    assert resp.status_code == 200
    endpoint_blocks = resp.json()

    local_blocks = parse_blocks(body)

    assert len(endpoint_blocks) == len(local_blocks)
    for ep, lp in zip(endpoint_blocks, local_blocks, strict=True):
        if isinstance(lp, ParagraphBlock):
            assert ep["type"] == "p"
            assert ep["text"] == lp.text
        else:
            assert ep["type"] == "img"
            assert ep["url"] == lp.url
