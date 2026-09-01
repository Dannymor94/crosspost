"""Media kind detection — image vs video, by file extension."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

_VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".m4v",
        ".3gp",
        ".flv",
        ".wmv",
    }
)


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


def detect_media_kind(path: Path | str) -> MediaKind:
    """Determine media kind from file extension. Case-insensitive, deterministic."""
    ext = Path(path).suffix.lower()
    return MediaKind.VIDEO if ext in _VIDEO_EXTENSIONS else MediaKind.IMAGE
