"""Tests for GET /public-media/{token}."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-pub-media-secret")
    monkeypatch.setenv("MEDIA_URL_TTL_SECONDS", "3600")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")


@pytest.fixture()
def media_dir(tmp_path) -> Path:
    d = tmp_path / "media"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path, media_dir):
    from crosspost.web.app import create_app

    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


def _sign(relative_path: str, ttl: int = 3600, now: float | None = None) -> str:
    from crosspost.web.signed_media import sign_media_path

    return sign_media_path(relative_path, ttl_seconds=ttl, now=now)


# 1. Valid token for an existing file → 200, exact bytes, correct content-type
def test_valid_token_serves_file(client, media_dir):
    data = b"\x00\x01\x02" * 100
    (media_dir / "clip.mp4").write_bytes(data)
    token = _sign("clip.mp4")
    r = client.get(f"/public-media/{token}")
    assert r.status_code == 200
    assert r.content == data
    assert "video" in r.headers["content-type"]


# 2. Tampered token → 404
def test_tampered_token_returns_404(client, media_dir):
    (media_dir / "clip.mp4").write_bytes(b"\x00" * 10)
    token = _sign("clip.mp4")
    bad = token[:-1] + ("X" if token[-1] != "X" else "Y")
    r = client.get(f"/public-media/{bad}")
    assert r.status_code == 404


# 3. Expired token → 404
def test_expired_token_returns_404(client, media_dir):
    (media_dir / "clip.mp4").write_bytes(b"\x00" * 10)
    past = time.time() - 7200
    token = _sign("clip.mp4", ttl=3600, now=past)
    r = client.get(f"/public-media/{token}")
    assert r.status_code == 404


# 4. Path traversal → 404
def test_path_traversal_returns_404(client):
    token = _sign("../../etc/passwd")
    r = client.get(f"/public-media/{token}")
    assert r.status_code == 404


# 5. Valid token, file deleted from disk → 404, no exception
def test_missing_file_returns_404(client, media_dir):
    (media_dir / "clip.mp4").write_bytes(b"\x00" * 10)
    token = _sign("clip.mp4")
    (media_dir / "clip.mp4").unlink()
    r = client.get(f"/public-media/{token}")
    assert r.status_code == 404


# 6. Route works without auth cookie
def test_no_auth_cookie_needed(client, media_dir):
    (media_dir / "clip.mp4").write_bytes(b"\x00" * 10)
    token = _sign("clip.mp4")
    # Explicitly send request with no cookies at all
    r = client.get(f"/public-media/{token}", cookies={})
    assert r.status_code == 200


# 8. uvicorn.access log must not contain the full token
def test_access_log_does_not_contain_full_token(media_dir, monkeypatch, tmp_path):
    """After a /public-media/<token> request uvicorn.access must not log the full token."""
    import logging

    monkeypatch.setenv("MEDIA_URL_SECRET", "test-pub-media-secret")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    (media_dir / "clip.mp4").write_bytes(b"\x00" * 10)
    token = _sign("clip.mp4")

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, r: logging.LogRecord) -> None:
            records.append(r)

    capture = _Capture()
    access_log = logging.getLogger("uvicorn.access")
    access_log.addHandler(capture)
    access_log.setLevel(logging.DEBUG)

    from crosspost.web.app import _ensure_app_logging

    _ensure_app_logging()  # installs _RedactPublicMediaFilter

    try:
        from fastapi.testclient import TestClient

        from crosspost.web.app import create_app

        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as c:
            c.get(f"/public-media/{token}")
    finally:
        access_log.removeHandler(capture)

    for record in records:
        msg = record.getMessage()
        assert token not in msg, f"full token in uvicorn.access log: {msg[:200]}"


# 9. HEAD with valid token → 200, Content-Length, no body
def test_head_valid_token(client, media_dir):
    data = b"\x00\x01\x02" * 100
    (media_dir / "clip.mp4").write_bytes(data)
    token = _sign("clip.mp4")
    r = client.head(f"/public-media/{token}")
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == len(data)
    assert "video" in r.headers["content-type"]
    assert r.content == b""


# 10. HEAD with tampered token → 404
def test_head_tampered_token_returns_404(client, media_dir):
    (media_dir / "clip.mp4").write_bytes(b"\x00" * 10)
    token = _sign("clip.mp4")
    bad = token[:-1] + ("X" if token[-1] != "X" else "Y")
    r = client.head(f"/public-media/{bad}")
    assert r.status_code == 404


# 7. Changing path inside a token breaks signature → 404
def test_path_swap_breaks_signature(client, media_dir):
    (media_dir / "a.mp4").write_bytes(b"\x00" * 10)
    (media_dir / "b.mp4").write_bytes(b"\x00" * 10)
    token_a = _sign("a.mp4")
    # Try to use token_a to serve b.mp4 by manually crafting a token
    # with b.mp4 but keeping token_a's signature → must fail
    import base64
    import json

    parts = token_a.encode().split(b".", 1)
    payload = json.loads(base64.urlsafe_b64decode(parts[0] + b"=="))
    payload["p"] = "b.mp4"
    new_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
    forged = (new_b64 + b"." + parts[1]).decode()
    r = client.get(f"/public-media/{forged}")
    assert r.status_code == 404
