"""Explicit-zone import rejects gaps/folds and past rows before persistence."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from crosspost.scheduler.import_plan import ImportReport, ResolvedRow
from crosspost.web.routes.import_plan import _resolve_import_times


def test_import_wall_time_resolution_preserves_valid_rows():
    values = [
        datetime(2027, 3, 28, 2, 30),
        datetime(2027, 10, 31, 2, 30),
        datetime(2026, 1, 1, 12),
        datetime(2027, 4, 1, 12),
    ]
    report = ImportReport(
        resolved=[
            ResolvedRow(i, "", "text", value, "explicit") for i, value in enumerate(values, 1)
        ]
    )
    _resolve_import_times(report, ZoneInfo("Europe/Berlin"), datetime(2027, 1, 1, tzinfo=UTC))
    assert [row.row_number for row in report.resolved] == [4]
    assert report.resolved[0].scheduled_at == datetime(2027, 4, 1, 10, tzinfo=UTC)
    assert [row.row_number for row in report.problems] == [1, 2, 3]
