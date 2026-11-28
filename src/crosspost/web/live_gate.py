"""Explicit Web and scheduler release gate; not a network sandbox."""

import os

from fastapi import HTTPException


def live_publication_enabled() -> bool:
    value = os.environ.get("CROSSPOST_LIVE_PUBLICATION_ENABLED")
    if value is not None:
        return value == "true"
    return os.environ.get("CROSSPOST_AUTH_MODE", "development").strip().lower() == "development"


def require_live_publication() -> None:
    if not live_publication_enabled():
        raise HTTPException(503, "Реальные публикации отключены на время пилота")
