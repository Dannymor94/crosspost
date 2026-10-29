"""Tests for signed_media — sign/verify, expiry, tamper detection, secret isolation."""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-secret-value-abc123")
    monkeypatch.setenv("MEDIA_URL_TTL_SECONDS", "3600")


def _sign_verify():
    from crosspost.web.signed_media import sign_media_path, verify_media_token

    return sign_media_path, verify_media_token


# 1. sign then verify returns the same relative_path
def test_sign_then_verify_roundtrip():
    sign, verify = _sign_verify()
    token = sign("pub-123/00_clip.mp4")
    assert verify(token) == "pub-123/00_clip.mp4"


# 2. One changed character → None
def test_tampered_token_returns_none():
    sign, verify = _sign_verify()
    token = sign("pub-123/00_clip.mp4")
    bad = token[:-1] + ("X" if token[-1] != "X" else "Y")
    assert verify(bad) is None


# 3. Expired token → None
def test_expired_token_returns_none():
    sign, verify = _sign_verify()
    past = time.time() - 7200
    token = sign("pub-123/00_clip.mp4", ttl_seconds=3600, now=past)
    assert verify(token) is None


# 4. Garbage string → None, no exception
def test_garbage_token_returns_none():
    _, verify = _sign_verify()
    assert verify("not.a.token.at.all") is None
    assert verify("") is None
    assert verify("aaaa") is None


# 5. Token signed with one secret does not verify under another
def test_wrong_secret_returns_none(monkeypatch):
    sign, verify = _sign_verify()
    token = sign("pub-123/00_clip.mp4")
    monkeypatch.setenv("MEDIA_URL_SECRET", "other-secret-xyz")
    # reload module functions with new env
    import importlib

    import crosspost.web.signed_media as m

    importlib.reload(m)
    assert m.verify_media_token(token) is None


# 6. App startup without MEDIA_URL_SECRET raises a clear error
def test_missing_secret_raises_on_startup(monkeypatch):
    monkeypatch.delenv("MEDIA_URL_SECRET", raising=False)
    import importlib

    import crosspost.web.signed_media as m

    importlib.reload(m)
    with pytest.raises(RuntimeError, match="MEDIA_URL_SECRET"):
        m.sign_media_path("some/path.mp4")


# 7. GET /media/<any path> returns 404 (static mount removed)
def test_media_static_mount_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-secret-value-abc123")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    # Create a real file that would have been served before
    media = tmp_path / "media"
    media.mkdir()
    (media / "clip.mp4").write_bytes(b"\x00" * 10)

    from fastapi.testclient import TestClient

    from crosspost.web.app import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/media/clip.mp4")
        assert r.status_code == 404


# 8. App starts WITHOUT MEDIA_URL_SECRET (lazy check — Instagram-less deploys boot)
def test_app_starts_without_media_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("MEDIA_URL_SECRET", raising=False)
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    from fastapi.testclient import TestClient

    from crosspost.web.app import create_app

    app = create_app()
    # Entering the context runs lifespan startup; it must NOT raise despite no secret.
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/tunnel-health").status_code == 200
