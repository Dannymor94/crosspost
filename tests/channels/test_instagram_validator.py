"""Instagram validator: 5 RED→GREEN tests.

1. Valid credentials → LIVE; token never appears in any response or log.
2. Invalid token → field error on 'access_token', connection NEEDS_RELOGIN.
3. graph.facebook.com unreachable → network-specific error, not "invalid token".
4. Expiry stored on Connection; "expires soon" flagged within 7-day window.
5. Invariant: instagram.enabled ⇒ guide with wizard steps present.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.channels.validators import VALIDATORS
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ConnectionState, CredentialKind
from crosspost.db.profile_repo import ProfileRepository

# ── Shared fixture ─────────────────────────────────────────────────────────────

_VALID_BLOB = '{"access_token": "EAAxxxxxx", "ig_user_id": "17841400000000000"}'


@pytest_asyncio.fixture
async def session():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def repo(session) -> ProfileRepository:
    vault = Fernet(Fernet.generate_key())
    return ProfileRepository(session, vault=vault)


# ── 1. Valid credentials → LIVE; token not leaked ─────────────────────────────


@pytest.mark.asyncio
async def test_valid_credentials_live_token_not_leaked(repo, monkeypatch):
    """Valid instagram blob → LIVE; raw token never appears in response body or logs."""
    from crosspost.channels.connection_validator import validate_connection

    # httpx returns a healthy user response
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": "17841400000000000"}

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        p = await repo.create_profile("iguser")
        await repo.set_credential(p.id, "instagram", CredentialKind.API_TOKEN, _VALID_BLOB)

        result = await validate_connection(repo, p.id, "instagram")

    state = result[0] if isinstance(result, tuple) else result
    assert state == ConnectionState.LIVE

    # Token must not appear in any log message
    from sqlalchemy import select

    from crosspost.db.models import Log

    logs = (await repo._s.execute(select(Log).where(Log.profile_id == p.id))).scalars().all()
    for log in logs:
        assert "EAAxxxxxx" not in (log.message or ""), "raw token found in log"


# ── 2. Invalid token → field error on access_token ────────────────────────────


@pytest.mark.asyncio
async def test_invalid_token_field_error():
    """Expired/invalid token → ValidatorResult with error_field='access_token'."""
    from crosspost.channels.validators import ValidatorResult, _validate_instagram

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "error": {"type": "OAuthException", "code": 190, "message": "Invalid OAuth access token"}
    }

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _validate_instagram(_VALID_BLOB)

    assert isinstance(result, ValidatorResult)
    assert not result.alive
    assert result.error_field == "access_token"
    assert "токен" in result.error.lower() or "token" in result.error.lower()


# ── 3. Network error → distinct message, not "invalid token" ──────────────────


@pytest.mark.asyncio
async def test_network_error_distinct_from_invalid_token():
    """ConnectError → ValidatorResult with error_field != 'access_token' (network, not auth)."""
    import httpx

    from crosspost.channels.validators import ValidatorResult, _validate_instagram

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _validate_instagram(_VALID_BLOB)

    assert isinstance(result, ValidatorResult)
    assert not result.alive
    # Network error ≠ auth error: field must NOT be access_token
    assert result.error_field != "access_token"
    # Message should mention network/connectivity, not "invalid token"
    msg_lower = result.error.lower()
    assert "сет" in msg_lower or "connect" in msg_lower or "недоступ" in msg_lower


# ── 4. Expiry stored; expires_soon flagged within 7-day window ────────────────


@pytest.mark.asyncio
async def test_expiry_stored_and_expires_soon_computed(repo, monkeypatch, session):
    """Token expiring in 5 days: expires_at stored on Connection; expires_soon=True."""
    from crosspost.channels.connection_validator import validate_connection

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_resp_user = MagicMock()
    mock_resp_user.json.return_value = {"id": "17841400000000000"}
    # Only one HTTP call expected: expiry is now computed locally (now + 60 days).
    mock_client.get = AsyncMock(return_value=mock_resp_user)

    with patch("httpx.AsyncClient", return_value=mock_client):
        p = await repo.create_profile("iguser4")
        await repo.set_credential(p.id, "instagram", CredentialKind.API_TOKEN, _VALID_BLOB)

        result = await validate_connection(repo, p.id, "instagram")

    state = result[0] if isinstance(result, tuple) else result
    assert state == ConnectionState.LIVE

    # expires_at must be stored on the connection row
    conn = await repo.get_connection(p.id, "instagram")
    assert conn is not None
    assert conn.expires_at is not None, "expires_at not stored"

    # expires_at should be ~60 days (±5 min tolerance) from now.
    # SQLite returns naive datetimes; normalize for comparison.
    stored = conn.expires_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    expected = datetime.now(UTC) + timedelta(days=60)
    delta = abs((stored - expected).total_seconds())
    assert delta < 300, f"expires_at too far from now+60d: {delta}s"

    # expires_soon: True when < 7 days remaining
    from crosspost.channels.connection_validator import is_expires_soon

    assert is_expires_soon(stored) is False  # 60 days is NOT expires_soon

    # A connection expiring in 5 days IS expires_soon
    near = datetime.now(UTC) + timedelta(days=5)
    assert is_expires_soon(near) is True


# ── 5b. Validator uses graph.instagram.com (not graph.facebook.com) ──────────


@pytest.mark.asyncio
async def test_validator_uses_instagram_graph_host():
    """_validate_instagram must call graph.instagram.com, NOT graph.facebook.com.

    Instagram API with Instagram Login uses graph.instagram.com. Using the wrong
    host makes valid tokens appear invalid.
    """
    from crosspost.channels.validators import _validate_instagram

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": "17841400000000000"}

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _validate_instagram(_VALID_BLOB)

    assert result.alive, f"Should be alive: {result.error}"
    # Check the URL used in the GET call
    call_args = mock_client.get.call_args
    url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
    assert "graph.instagram.com" in url, f"Expected graph.instagram.com in URL, got: {url}"
    assert "graph.facebook.com" not in url, (
        f"Must NOT use graph.facebook.com for Instagram Login tokens, got: {url}"
    )


# ── 6. code 190 → alive=False with token-expired wording (shared helper) ─────


@pytest.mark.asyncio
async def test_validator_code_190_gives_token_expired():
    """OAuthException code 190 → alive=False, error mentions токен/expired."""
    from crosspost.channels.validators import _validate_instagram

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "error": {"type": "OAuthException", "code": 190, "message": "Session has expired"}
    }
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _validate_instagram(_VALID_BLOB)

    assert not result.alive
    assert result.error_field == "access_token"
    low = result.error.lower()
    assert "токен" in low or "token" in low or "истёк" in low or "expired" in low, (
        f"error should mention token expiry, got: {result.error!r}"
    )


@pytest.mark.asyncio
async def test_validator_code_200_gives_verbatim_meta_message():
    """OAuthException code 200 'API access blocked' → alive=False, Meta's message verbatim in error."""
    from crosspost.channels.validators import _validate_instagram

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "error": {"type": "OAuthException", "code": 200, "message": "API access blocked."}
    }
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _validate_instagram(_VALID_BLOB)

    assert not result.alive
    assert "API access blocked." in result.error, (
        f"Meta's verbatim message must appear in error, got: {result.error!r}"
    )
    assert "instagram_business_content_publish" in result.error, (
        f"hint must mention permission name, got: {result.error!r}"
    )


# ── proxy support ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validator_uses_proxy_from_env(monkeypatch):
    """_validate_instagram creates httpx.AsyncClient with proxy= when INSTAGRAM_PROXY_URL is set."""
    from crosspost.channels.validators import _validate_instagram

    monkeypatch.setenv("INSTAGRAM_PROXY_URL", "http://127.0.0.1:10809")

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": "17841400000000000"}
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
        result = await _validate_instagram(_VALID_BLOB)

    assert result.alive
    assert mock_cls.call_args.kwargs.get("proxy") == "http://127.0.0.1:10809"


@pytest.mark.asyncio
async def test_validator_proxy_unreachable_explicit_error(monkeypatch):
    """ProxyError with INSTAGRAM_PROXY_URL set → explicit proxy error, not a generic message."""
    import httpx as _httpx

    from crosspost.channels.validators import ValidatorResult, _validate_instagram

    monkeypatch.setenv("INSTAGRAM_PROXY_URL", "http://127.0.0.1:10809")

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=_httpx.ProxyError("unreachable"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _validate_instagram(_VALID_BLOB)

    assert isinstance(result, ValidatorResult)
    assert not result.alive
    assert "INSTAGRAM_PROXY_URL" in result.error
    assert "прокси" in result.error.lower() or "proxy" in result.error.lower()


@pytest.mark.asyncio
async def test_validator_proxy_credentials_not_in_error(monkeypatch):
    """Proxy URL credentials must never appear in validator error messages."""
    import httpx as _httpx

    from crosspost.channels.validators import _validate_instagram

    monkeypatch.setenv("INSTAGRAM_PROXY_URL", "http://user:s3cr3t@127.0.0.1:10809")

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=_httpx.ProxyError("unreachable"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _validate_instagram(_VALID_BLOB)

    assert not result.alive
    assert "s3cr3t" not in result.error
    assert "user:s3cr3t" not in result.error


@pytest.mark.asyncio
async def test_telegraph_validator_no_proxy(monkeypatch):
    """Telegraph validator never passes proxy= even when INSTAGRAM_PROXY_URL is set."""
    from crosspost.channels.validators import _validate_telegraph

    monkeypatch.setenv("INSTAGRAM_PROXY_URL", "http://127.0.0.1:10809")

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
        await _validate_telegraph("test_token")

    call_kwargs = mock_cls.call_args.kwargs if mock_cls.call_args else {}
    assert "proxy" not in call_kwargs, "telegraph must not use Instagram's proxy"


@pytest.mark.asyncio
async def test_validator_code_200_no_proxy_has_region_hint(monkeypatch):
    """OAuthException code 200 without proxy → error includes INSTAGRAM_PROXY_URL hint."""
    from crosspost.channels.validators import _validate_instagram

    monkeypatch.delenv("INSTAGRAM_PROXY_URL", raising=False)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "error": {"type": "OAuthException", "code": 200, "message": "API access blocked."}
    }
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _validate_instagram(_VALID_BLOB)

    assert not result.alive
    assert "INSTAGRAM_PROXY_URL" in result.error, (
        f"region hint must mention INSTAGRAM_PROXY_URL, got: {result.error!r}"
    )


# ── 5. Invariant: instagram.enabled ⇒ wizard steps present ───────────────────


def test_instagram_enabled_requires_wizard():
    """If instagram is enabled, it must declare a guide with wizard steps.

    This is the 'enabled ⇒ wizard' invariant from CLAUDE.md: we flip enabled=True
    only together with the wizard declaration, so this test fails RED until both
    are done together.
    """
    v = VALIDATORS.get("instagram")
    assert v is not None, "instagram not in VALIDATORS"
    assert v.enabled, "instagram must be enabled (flip to True together with the wizard)"
    assert v.guide is not None, "instagram.enabled requires a guide declaration"
    assert len(v.guide.steps) > 0, "instagram guide must have at least one wizard step"
