"""Regression coverage for instant normalization and non-destructive time audit."""

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from crosspost.scheduler.schedule_time import (
    legacy_schedule_time,
    parse_instant,
    wall_candidates,
)
from crosspost.scheduler.time_audit import audit


def test_explicit_instants_match_across_offsets():
    expected = datetime(2027, 1, 1, 10, tzinfo=UTC)
    assert parse_instant("2027-01-01T15:30:00+05:30") == expected
    assert parse_instant("2027-01-01T05:00:00-05:00") == expected
    with pytest.raises(ValueError):
        parse_instant("2027-01-01T10:00:00")


def test_legacy_offset_conversion_and_naive_compatibility():
    expected = datetime(2027, 1, 1, 10, tzinfo=UTC).astimezone().replace(tzinfo=None)
    assert legacy_schedule_time("2027-01-01T15:30:00+05:30") == expected
    assert legacy_schedule_time("2027-01-01T05:00:00-05:00") == expected
    assert legacy_schedule_time("2027-01-01T10:00:00") == datetime(2027, 1, 1, 10)


def test_berlin_gap_and_fold():
    zone = ZoneInfo("Europe/Berlin")
    assert wall_candidates(datetime(2026, 3, 29, 2, 30), zone) == ()
    assert wall_candidates(datetime(2026, 10, 25, 2, 30), zone) == (
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
    )
    assert wall_candidates(datetime(2026, 11, 25, 12), zone) == (
        datetime(2026, 11, 25, 11, tzinfo=UTC),
    )


def test_audit_does_not_modify_backup(tmp_path):
    path = tmp_path / "backup.sqlite"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE scheduled_posts "
            "(id INTEGER, profile_id INTEGER, status TEXT, scheduled_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO scheduled_posts VALUES (?, 1, ?, ?)",
            [
                (1, "scheduled", "2026-03-29T02:30:00"),
                (2, "pending_approval", "2026-10-25T02:30:00"),
                (3, "scheduled", "2027-01-01T10:00:00"),
                (4, "scheduled", "2027-01-01T10:00:00+00:00"),
                (5, "failed", "bad"),
                (6, "done", "2027-01-01T10:00:00"),
            ],
        )
        conn.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = audit(path, "Europe/Berlin")
    assert report["migration_authorized"] is False
    assert [row["classification"] for row in report["rows"]] == [
        "gap",
        "fold",
        "unverified_assumption",
        "explicit_offset",
        "invalid",
    ]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_utc_due_ignores_legacy_wall_time():
    from crosspost.scheduler.schedule_time import schedule_is_due

    wall = datetime(2027, 1, 1, 23)
    instant = datetime(2027, 1, 1, 10)
    assert not schedule_is_due(wall, instant, datetime(2027, 1, 1, 9, 59, tzinfo=UTC))
    assert schedule_is_due(wall, instant, datetime(2027, 1, 1, 10, tzinfo=UTC))


def test_audit_recognizes_canonical_utc_without_guessing(tmp_path):
    path = tmp_path / "canonical.sqlite"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE scheduled_posts "
            "(id INTEGER, profile_id INTEGER, status TEXT, scheduled_at TEXT, scheduled_at_utc TEXT)"
        )
        conn.execute(
            "INSERT INTO scheduled_posts VALUES (1, 1, 'scheduled', ?, ?)",
            ("2027-01-01T12:00:00", "2027-01-01T10:00:00"),
        )
        conn.commit()
    row = audit(path, "Europe/Berlin")["rows"][0]
    assert row["classification"] == "authoritative_utc"
    assert row["utc_candidates"] == ["2027-01-01T10:00:00+00:00"]
