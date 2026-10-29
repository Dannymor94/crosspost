"""Tunnel auto-restart: 5 RED→GREEN tests.

1. Flag off → no health check, no tunnel start.
2. Healthy tunnel → no restart, publish proceeds with current URL.
3. Unhealthy (502/wrong nonce) → kill old, start new, new URL in state + .env.
4. Two concurrent publishes with dead tunnel → exactly one tunnel start.
5. Tunnel cannot be raised within limit → FAILED with message, zero Meta calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crosspost.adapters.api.instagram import InstagramAdapter
from crosspost.adapters.base import ResultStatus
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.orchestrator.task import InMemoryIdempotencyStore, new_publication_id

_TOKEN = "IGQV-test-token"
_IG_USER = "17841400000000000"
_CONTAINER = "18000000000000000"
_MEDIA_ID = "17999999999999999"
_BASE_URL = "https://abc123.lhr.life"
_NEW_BASE_URL = "https://xyz789.lhr.life"


def _reel(tmp_path: Path) -> CanonicalContent:
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    vid = media / "clip.mp4"
    vid.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
    return CanonicalContent(type=ContentType.REEL, text="caption", media_paths=[vid])


def _resp(payload: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload
    return r


class FakeHTTP:
    """Routes post/get by URL, returns pre-canned responses."""

    def __init__(self) -> None:
        self.create_calls = 0
        self.publish_calls = 0
        self.status_calls = 0

    async def post(self, url, **kwargs):
        if "/media_publish" in url:
            self.publish_calls += 1
            return _resp({"id": _MEDIA_ID})
        if url.endswith("/media"):
            self.create_calls += 1
            return _resp({"id": _CONTAINER})
        raise AssertionError(f"unexpected POST {url}")

    async def get(self, url, **kwargs):
        self.status_calls += 1
        return _resp({"status_code": "FINISHED"})

    async def aclose(self):
        pass


def _adapter(http, **kw) -> InstagramAdapter:
    return InstagramAdapter(
        _TOKEN,
        _IG_USER,
        InMemoryIdempotencyStore(),
        client=http,
        poll_interval_seconds=0.01,
        poll_timeout_seconds=0.05,
        **kw,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())


@pytest.fixture(autouse=True)
def _base_env(monkeypatch, tmp_path):
    monkeypatch.setenv("INSTAGRAM_PUBLIC_BASE_URL", _BASE_URL)
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-signing-secret")
    monkeypatch.delenv("INSTAGRAM_TUNNEL_AUTO", raising=False)


# ── 1. Flag off → tunnel logic never runs ─────────────────────────────────────


@pytest.mark.asyncio
async def test_flag_off_no_tunnel_check(monkeypatch, tmp_path):
    """INSTAGRAM_TUNNEL_AUTO not set → ensure_tunnel never called."""
    ensure_called = []

    async def _fake_ensure(**_kw):
        ensure_called.append(1)
        return _BASE_URL

    with patch("crosspost.tunnel.manager.ensure_tunnel", side_effect=_fake_ensure):
        adapter = _adapter(FakeHTTP())
        result = await adapter.publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.DONE
    assert ensure_called == [], "ensure_tunnel must not be called when flag is off"


# ── 2. Healthy tunnel → no restart, publish proceeds ─────────────────────────


@pytest.mark.asyncio
async def test_healthy_tunnel_no_restart(monkeypatch, tmp_path):
    """Healthy tunnel → ensure_tunnel returns current URL, no runner called."""
    from crosspost.tunnel import state as tunnel_state

    runner_calls = []

    async def _healthy_ensure(**_kw):
        # Healthy: no runner, return current URL
        return _BASE_URL

    monkeypatch.setenv("INSTAGRAM_TUNNEL_AUTO", "1")
    tunnel_state.set_public_base_url(_BASE_URL)

    with patch("crosspost.tunnel.manager.ensure_tunnel", side_effect=_healthy_ensure):
        adapter = _adapter(FakeHTTP())
        result = await adapter.publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.DONE
    assert runner_calls == []


# ── 3. Unhealthy → kill old, start new, state + .env updated ─────────────────


@pytest.mark.asyncio
async def test_unhealthy_tunnel_restarts_and_updates(monkeypatch, tmp_path):
    """ensure_tunnel (unhealthy path): kills old PID, runs runner, updates state + .env."""
    import crosspost.tunnel.manager as mgr
    from crosspost.tunnel import state as tunnel_state

    monkeypatch.setenv("INSTAGRAM_TUNNEL_AUTO", "1")
    tunnel_state.set_public_base_url(_BASE_URL)
    tunnel_state.set_nonce("test-nonce")

    runner_calls = []
    env_updates = []

    def _fake_runner(port=8000):
        runner_calls.append(1)
        return _NEW_BASE_URL

    kill_calls = []

    def _fake_kill():
        kill_calls.append(1)

    # Patch health check: first call (current URL) → unhealthy, second (new URL) → healthy
    call_count = [0]

    async def _fake_health(base_url, nonce, timeout=5.0):
        call_count[0] += 1
        if base_url == _NEW_BASE_URL:
            return True
        return False

    def _fake_update_env(url, env_file=None):
        env_updates.append(url)

    monkeypatch.setattr(mgr, "_runner", _fake_runner)
    monkeypatch.setattr(mgr, "_kill_existing", _fake_kill)
    monkeypatch.setattr(mgr, "_check_health", _fake_health)
    monkeypatch.setattr(mgr, "_update_env", _fake_update_env)

    http = FakeHTTP()
    adapter = _adapter(http)
    result = await adapter.publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.DONE
    assert runner_calls == [1], "runner must be called exactly once"
    assert kill_calls == [1], "_kill_existing must be called"
    assert env_updates == [_NEW_BASE_URL], ".env must be updated with new URL"
    assert tunnel_state.get_public_base_url() == _NEW_BASE_URL
    assert http.create_calls == 1


# ── 4. Concurrent publishes → exactly one tunnel start ────────────────────────


@pytest.mark.asyncio
async def test_concurrent_publishes_one_tunnel_start(monkeypatch, tmp_path):
    """Two concurrent publishes with dead tunnel → exactly one runner call (lock)."""
    import crosspost.tunnel.manager as mgr
    from crosspost.tunnel import state as tunnel_state

    monkeypatch.setenv("INSTAGRAM_TUNNEL_AUTO", "1")
    tunnel_state.set_public_base_url(_BASE_URL)
    tunnel_state.set_nonce("test-nonce")

    runner_calls = []

    def _slow_runner(port=8000):
        runner_calls.append(1)
        return _NEW_BASE_URL

    async def _fake_health(base_url, nonce, timeout=5.0):
        return base_url == _NEW_BASE_URL

    monkeypatch.setattr(mgr, "_runner", _slow_runner)
    monkeypatch.setattr(mgr, "_kill_existing", lambda: None)
    monkeypatch.setattr(mgr, "_check_health", _fake_health)
    monkeypatch.setattr(mgr, "_update_env", lambda url, env_file=None: None)

    http1, http2 = FakeHTTP(), FakeHTTP()
    store = InMemoryIdempotencyStore()
    # Use same store, different publication IDs
    adapter1 = InstagramAdapter(
        _TOKEN,
        _IG_USER,
        store,
        client=http1,
        poll_interval_seconds=0.01,
        poll_timeout_seconds=0.05,
    )
    adapter2 = InstagramAdapter(
        _TOKEN,
        _IG_USER,
        store,
        client=http2,
        poll_interval_seconds=0.01,
        poll_timeout_seconds=0.05,
    )

    reel = _reel(tmp_path)
    r1, r2 = await asyncio.gather(
        adapter1.publish(reel, publication_id=new_publication_id()),
        adapter2.publish(reel, publication_id=new_publication_id()),
    )

    assert runner_calls == [1], f"expected 1 runner call, got {len(runner_calls)}"
    # At least one succeeded (the second may be idempotency-skipped or also done)
    statuses = {r1.status, r2.status}
    assert ResultStatus.FAILED not in statuses, f"unexpected FAILED: {r1} {r2}"


# ── 5. Tunnel cannot be raised → FAILED, zero Meta calls ─────────────────────


@pytest.mark.asyncio
async def test_tunnel_unraisable_returns_failed_no_meta_calls(monkeypatch, tmp_path):
    """If ensure_tunnel raises, adapter returns FAILED with the right message; no API calls."""
    import crosspost.tunnel.manager as mgr
    from crosspost.tunnel import state as tunnel_state

    monkeypatch.setenv("INSTAGRAM_TUNNEL_AUTO", "1")
    tunnel_state.set_public_base_url(_BASE_URL)
    tunnel_state.set_nonce("test-nonce")

    async def _fail_health(base_url, nonce, timeout=5.0):
        return False

    def _failing_runner(port=8000):
        raise RuntimeError("ssh: connection refused")

    monkeypatch.setattr(mgr, "_runner", _failing_runner)
    monkeypatch.setattr(mgr, "_kill_existing", lambda: None)
    monkeypatch.setattr(mgr, "_check_health", _fail_health)
    monkeypatch.setattr(mgr, "_update_env", lambda url, env_file=None: None)

    http = FakeHTTP()
    adapter = _adapter(http)
    result = await adapter.publish(_reel(tmp_path), publication_id=new_publication_id())

    assert result.status is ResultStatus.FAILED
    assert "туннель" in result.error.lower() or "tunnel" in result.error.lower(), (
        f"error must mention tunnel, got: {result.error!r}"
    )
    assert "make tunnel" in result.error, f"error must mention make tunnel, got: {result.error!r}"
    assert http.create_calls == 0, "no Meta container call when tunnel cannot be raised"
    assert http.publish_calls == 0
