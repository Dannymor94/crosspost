"""Media retention for submitted publications (T-209).

Media is kept while any channel is in SUBMITTED state; deletion is blocked
at the finalization call sites in publish.py and executor.py.

This module provides TTL-based cleanup for submitted media — called by a
background task (T-210 / future) once per sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from crosspost.web import media as media_store

SUBMITTED_MEDIA_TTL_DAYS_DEFAULT = 14  # Yandex moderation takes up to 7 days; 14 gives margin


def submitted_media_ttl_days() -> int:
    """Return TTL in days from env, falling back to default."""
    import os

    raw = os.environ.get("SUBMITTED_MEDIA_TTL_DAYS", "")
    try:
        return int(raw) if raw else SUBMITTED_MEDIA_TTL_DAYS_DEFAULT
    except ValueError:
        return SUBMITTED_MEDIA_TTL_DAYS_DEFAULT


def is_submitted_media_expired(submitted_at: datetime, ttl_days: int | None = None) -> bool:
    """True when submitted_at is older than ttl_days (default from env)."""
    days = ttl_days if ttl_days is not None else submitted_media_ttl_days()
    cutoff = submitted_at.astimezone(UTC) + timedelta(days=days)
    return datetime.now(UTC) >= cutoff


def cleanup_submitted_media_if_expired(
    key: str,
    submitted_at: datetime,
    ttl_days: int | None = None,
) -> bool:
    """Delete media dir for a submitted post if past TTL. Returns True if deleted."""
    if is_submitted_media_expired(submitted_at, ttl_days):
        media_store.cleanup_media(key)
        return True
    return False
