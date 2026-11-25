"""T-217: explicit instant conversion and compatibility with legacy local storage."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def parse_instant(value: str) -> datetime:
    """Normalize an explicitly offset-bearing ISO datetime to UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("An explicit UTC offset is required")
    return parsed.astimezone(UTC)


def wall_candidates(wall: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    """Return zero, one or two valid UTC instants for an IANA local wall time.

    Round-trip validation excludes nonexistent times in a DST gap. Distinct
    instants during a fold remain distinct; no default fold is selected.
    """
    if wall.tzinfo is not None:
        raise ValueError("Expected a naive local wall time")
    candidates = set()
    for fold in (0, 1):
        instant = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == wall:
            candidates.add(instant)
    return tuple(sorted(candidates))


def legacy_schedule_time(value: str) -> datetime:
    """Use the CURRENT server-local storage convention, not UTC storage.

    Legacy naive values are preserved for compatibility. Offset-bearing inputs
    are converted before SQLite drops the offset. Refuse a server-local fold
    because SQLite cannot distinguish the two occurrences in this schema.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        return parsed
    instant = parsed.astimezone(UTC)
    wall = instant.astimezone().replace(tzinfo=None, fold=0)
    first = wall.astimezone(UTC)
    second = wall.replace(fold=1).astimezone(UTC)
    if first != instant or second != instant:
        raise ValueError("Ambiguous server-local time; choose another time")
    return wall
