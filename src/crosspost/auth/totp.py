"""RFC 4226/6238 primitives for owner MFA; no enrollment or login policy here."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct

STEP_SECONDS = 30
DIGITS = 6


def generate_secret() -> str:
    """160-bit random secret, unpadded Base32 for authenticator apps."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def _decode_secret(secret: str) -> bytes:
    if not re.fullmatch(r"[A-Z2-7]{32}", secret):
        raise ValueError("Invalid authenticator secret")
    return base64.b32decode(secret)


def hotp(secret: str, counter: int, *, digits: int = DIGITS) -> str:
    if not 0 <= counter < 2**64 or digits not in (6, 8):
        raise ValueError("Invalid HOTP parameters")
    digest = hmac.new(_decode_secret(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def match_step(secret: str, code: str, *, now: int, last_step: int = -1) -> int | None:
    """Match within ±1 step; caller must atomically advance last_step in DB."""
    if now < 0 or not re.fullmatch(r"[0-9]{6}", code):
        return None
    current = now // STEP_SECONDS
    matched = None
    for step in (current - 1, current, current + 1):
        if step >= 0 and hmac.compare_digest(hotp(secret, step), code) and step > last_step:
            matched = step
    return matched


def generate_backup_codes(count: int = 8) -> list[str]:
    """High-entropy one-use recovery capabilities; return plaintext only once."""
    if not 1 <= count <= 16:
        raise ValueError("Invalid backup code count")
    return [secrets.token_urlsafe(16) for _ in range(count)]
