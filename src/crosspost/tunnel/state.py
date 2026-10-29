"""Runtime state for the Instagram tunnel: current public base URL and per-process nonce."""

from __future__ import annotations

import os

_url: str = ""
_nonce: str = ""


def get_public_base_url() -> str:
    """Return current public base URL; falls back to INSTAGRAM_PUBLIC_BASE_URL env."""
    return _url or os.environ.get("INSTAGRAM_PUBLIC_BASE_URL", "")


def set_public_base_url(url: str) -> None:
    global _url
    _url = url.rstrip("/")


def get_nonce() -> str:
    return _nonce


def set_nonce(n: str) -> None:
    global _nonce
    _nonce = n
