"""Signed, expiring URLs for media files served to external parties (e.g. Meta).

sign_media_path()  → opaque token embedding path + expiry, HMAC-SHA256 signed.
verify_media_token() → relative_path on success, None on any failure.

Secret: MEDIA_URL_SECRET env var (separate from SESSION_SECRET).
TTL:    MEDIA_URL_TTL_SECONDS env var, default 3600.

Usage:
    token = sign_media_path("pub-123/00_clip.mp4")
    path  = verify_media_token(token)  # None if expired or tampered
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time


def _secret() -> bytes:
    """Read MEDIA_URL_SECRET. Raises RuntimeError if missing — same style as VAULT_KEY."""
    val = os.environ.get("MEDIA_URL_SECRET", "").strip()
    if not val:
        raise RuntimeError(
            "MEDIA_URL_SECRET не задан. Задайте env-переменную.\n"
            'Сгенерировать: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    return val.encode()


def _ttl() -> int:
    return int(os.environ.get("MEDIA_URL_TTL_SECONDS", "3600"))


def sign_media_path(
    relative_path: str, ttl_seconds: int | None = None, now: float | None = None
) -> str:
    """Return a URL-safe token for relative_path, valid for ttl_seconds."""
    if ttl_seconds is None:
        ttl_seconds = _ttl()
    ts = (now if now is not None else time.time()) + ttl_seconds
    payload = json.dumps({"p": relative_path, "e": ts}, separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(payload)
    sig = hmac.new(_secret(), b64, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig)
    return (b64 + b"." + sig_b64).decode()


def verify_media_token(token: str, now: float | None = None) -> str | None:
    """Return relative_path if token is valid and not expired, else None."""
    try:
        parts = token.encode().split(b".", 1)
        if len(parts) != 2:
            return None
        b64, sig_b64 = parts
        expected_sig = hmac.new(_secret(), b64, hashlib.sha256).digest()
        given_sig = base64.urlsafe_b64decode(sig_b64 + b"==")  # pad for safety
        if not hmac.compare_digest(expected_sig, given_sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(b64 + b"=="))
        ts = now if now is not None else time.time()
        if ts > data["e"]:
            return None
        return str(data["p"])
    except Exception:
        return None
