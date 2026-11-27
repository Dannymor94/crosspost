"""Order UTC schedules without interpreting historical local timestamps."""

from crosspost.db.models import ScheduledPost


def order_schedules(rows: list[ScheduledPost]) -> list[ScheduledPost]:
    """Sort canonical instants within the existing wall-time queue slots.

    Legacy rows keep their wall-time positions and relative order. Their UTC
    instants are unknown, so a global mixed chronological order is not claimed.
    Canonical rows are always in instant/id order, including across a DST fold.
    """
    wall_order = sorted(rows, key=lambda row: (row.scheduled_at, row.id))
    canonical = iter(
        sorted(
            (row for row in wall_order if row.scheduled_at_utc is not None),
            key=lambda row: (row.scheduled_at_utc or row.scheduled_at, row.id),
        )
    )
    return [row if row.scheduled_at_utc is None else next(canonical) for row in wall_order]
