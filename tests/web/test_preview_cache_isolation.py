"""Security tests: cache isolation in the preview-blocks endpoint.

Design: parse_blocks only creates ImageBlock for http(s):// URLs (via _is_sole_url),
so folder-relative filenames always become ParagraphBlocks and never reach the cache.
_fetch_preview_data_uri adds an explicit guard for defense-in-depth.
Cache is URL-keyed and safe because only publicly-verified http(s) URLs enter it.

Tests:
1. Same filename in two profile contexts → both paragraph blocks, no cross-profile image data
2. Same public URL twice → fetch_media called once (cache hit on second call)
3. SSRF host → error block, nothing cached
4. Oversized image → error block, nothing cached
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

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

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "test-secret-please-change")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))


@pytest_asyncio.fixture
async def factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(f)
    yield f
    await engine.dispose()
    publish._RUNS.clear()
    publish._preview_url_cache.clear()


@pytest_asyncio.fixture
async def admin_client(factory) -> AsyncGenerator[AsyncClient, None]:
    from crosspost.auth.sessions import sign_session
    from crosspost.db.models import Role
    from crosspost.db.user_repo import UserRepository

    @asynccontextmanager
    async def _lifespan(app):
        yield

    app = FastAPI(lifespan=_lifespan)
    for r in (profiles, channels, tg_login, publish):
        app.include_router(r.router)

    async with factory() as s:
        admin = await UserRepository(s).create_user("admin", "pw", role=Role.ADMIN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.set("session", sign_session(admin.id))
        yield c


@pytest_asyncio.fixture
async def profile_a(factory) -> int:
    async with factory() as s:
        p = await ProfileRepository(s, vault=get_vault()).create_profile("client-a")
        return p.id


@pytest_asyncio.fixture
async def profile_b(factory) -> int:
    async with factory() as s:
        p = await ProfileRepository(s, vault=get_vault()).create_profile("client-b")
        return p.id


_URL = "/api/profiles/{pid}/publish/preview-blocks"


# ── 1. folder-relative filename → paragraph block, no cross-profile image ─────


async def test_filename_becomes_paragraph_not_image(admin_client, profile_a, profile_b):
    """Bare filename on its own line is a paragraph block, never an image.

    parse_blocks only creates ImageBlock for http(s):// URLs (_is_sole_url).
    A filename like 'photo.jpg' starts with no scheme → ParagraphBlock.
    Neither profile can resolve the other's folder content through preview.
    """
    for pid in (profile_a, profile_b):
        resp = await admin_client.post(_URL.format(pid=pid), data={"text": "photo.jpg"})
        assert resp.status_code == 200
        blocks = resp.json()
        assert len(blocks) == 1
        assert blocks[0]["type"] == "p", f"profile {pid}: filename became image block"
        assert blocks[0].get("data_uri") is None
        assert blocks[0].get("url") is None


async def test_filename_in_multiblock_text_stays_paragraph(admin_client, profile_a):
    """Filename mixed with real content: both stay paragraph blocks."""
    body = "Введение.\n\nreport.jpg\n\nЗаключение."
    resp = await admin_client.post(_URL.format(pid=profile_a), data={"text": body})
    assert resp.status_code == 200
    blocks = resp.json()
    assert all(b["type"] == "p" for b in blocks)


# ── 2. same public URL twice → fetch_media called once (cache hit) ────────────


async def test_cache_hit_for_same_public_url(admin_client, profile_a, monkeypatch):
    """Second preview call with the same http URL serves from cache; fetch_media not re-called."""
    image_bytes = b"\x89PNG\r\nDATA"
    call_count = {"n": 0}

    async def counting_fetch(url, *, client=None, max_bytes=None, **kw):
        call_count["n"] += 1
        from crosspost.scheduler.media_sources import FetchedMedia, SourceKind

        return FetchedMedia(data=image_bytes, filename="img.png", kind=SourceKind.HTTP)

    monkeypatch.setattr(publish, "fetch_media", counting_fetch)

    body = "https://example.com/img.png"

    resp1 = await admin_client.post(_URL.format(pid=profile_a), data={"text": body})
    assert resp1.status_code == 200
    assert resp1.json()[0]["type"] == "img"
    assert resp1.json()[0]["data_uri"] is not None

    resp2 = await admin_client.post(_URL.format(pid=profile_a), data={"text": body})
    assert resp2.status_code == 200
    assert resp2.json()[0]["data_uri"] is not None

    assert call_count["n"] == 1, (
        f"fetch_media called {call_count['n']} times; expected 1 (cache hit)"
    )


# ── 3. SSRF host → error block, nothing cached ───────────────────────────────


async def test_ssrf_not_cached(admin_client, profile_a, monkeypatch):
    """SSRF-blocked URL returns error block; URL must NOT enter the cache."""
    monkeypatch.setattr(
        "crosspost.scheduler.media_sources._default_resolve",
        lambda _host: ["10.0.0.1"],
    )

    body = "http://internal.host/secret"
    resp = await admin_client.post(_URL.format(pid=profile_a), data={"text": body})
    assert resp.status_code == 200
    blocks = resp.json()
    assert blocks[0]["type"] == "img"
    assert blocks[0]["error"] is not None
    assert blocks[0]["data_uri"] is None

    # Nothing poisoned the cache.
    assert body not in publish._preview_url_cache


# ── defense-in-depth: _fetch_preview_data_uri rejects non-http scheme ────────


async def test_non_http_url_rejected_by_guard():
    """_fetch_preview_data_uri raises MediaSourceError for any non-http(s) URL.

    This is the explicit guard — it fires even if parse_blocks is bypassed.
    """
    from crosspost.scheduler.media_sources import MediaSourceError
    from crosspost.web.routes.publish import _fetch_preview_data_uri

    with pytest.raises(MediaSourceError, match="http"):
        await _fetch_preview_data_uri("file:///etc/passwd", client=None)  # type: ignore[arg-type]

    with pytest.raises(MediaSourceError, match="http"):
        await _fetch_preview_data_uri("/abs/path/photo.jpg", client=None)  # type: ignore[arg-type]


# ── 4. oversized image → error block, nothing cached ─────────────────────────


async def test_oversized_image_not_cached(admin_client, profile_a, monkeypatch):
    """Image larger than the 1 MB preview cap → error block; URL NOT cached."""
    from crosspost.scheduler.media_sources import MediaSourceError

    async def oversized_fetch(url, *, client=None, max_bytes=None, **kw):
        raise MediaSourceError("Файл превышает допустимый размер.")

    monkeypatch.setattr(publish, "fetch_media", oversized_fetch)

    url = "https://example.com/huge.jpg"
    body = url
    resp = await admin_client.post(_URL.format(pid=profile_a), data={"text": body})
    assert resp.status_code == 200
    blocks = resp.json()
    assert blocks[0]["type"] == "img"
    assert blocks[0]["error"] is not None
    assert blocks[0]["data_uri"] is None

    assert url not in publish._preview_url_cache
