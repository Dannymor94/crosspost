"""Read-only legacy inventory. Run on a verified backup, never migrate implicitly."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from crosspost.scheduler.schedule_time import parse_instant, wall_candidates


def audit(path: Path, assumed_zone: str) -> dict:
    """List active rows and candidate instants; assumption is NOT provenance."""
    zone = ZoneInfo(assumed_zone)
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    rows = []
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        result = conn.execute(
            "SELECT id, profile_id, status, scheduled_at FROM scheduled_posts "
            "WHERE status IN ('scheduled','pending_approval','publishing','failed') "
            "ORDER BY id"
        )
        for row_id, profile_id, status, value in result:
            candidates: tuple[datetime, ...] = ()
            try:
                if not isinstance(value, str):
                    raise ValueError("Invalid timestamp type")
                parsed = datetime.fromisoformat(value)
                if parsed.utcoffset() is not None:
                    candidates = (parse_instant(value),)
                    classification = "explicit_offset"
                else:
                    candidates = wall_candidates(parsed, zone)
                    classification = {
                        0: "gap",
                        1: "unverified_assumption",
                        2: "fold",
                    }[len(candidates)]
            except (ValueError, OverflowError):
                classification = "invalid"
            rows.append(
                {
                    "id": row_id,
                    "profile_id": profile_id,
                    "status": status,
                    "classification": classification,
                    "utc_candidates": [candidate.isoformat() for candidate in candidates],
                }
            )
    return {
        "assumed_zone": assumed_zone,
        "migration_authorized": False,
        "warning": "Naive timestamps have no proven source timezone; review every row.",
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Verified SQLite backup")
    parser.add_argument("--assumed-zone", required=True, help="Explicit IANA timezone hypothesis")
    args = parser.parse_args()
    print(json.dumps(audit(args.db, args.assumed_zone), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
