"""Shared overview read model, including explicit legacy time and malformed payloads."""

from pathlib import Path

from playwright.sync_api import sync_playwright

FRONTEND = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend"


def test_overview_summary_preserves_server_order_and_rejects_bad_data():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.add_script_tag(path=FRONTEND / "schedule-time.js")
            page.add_script_tag(path=FRONTEND / "shared/ux-overview.js")
            result = page.evaluate(
                """() => {
                  const api = window.CrosspostOverview;
                  const rows = [
                    {id:2,status:"scheduled",text:"A",scheduled_at:"2099-01-01T10:00:00Z",
                     time_basis:"utc"},
                    {id:1,status:"pending_approval",text:"B",
                     scheduled_at:"2099-01-01T09:00:00",time_basis:"legacy_local"}
                  ];
                  const summary=api.summarize(rows);
                  const rejected=[null,{},[rows[0],rows[0]],[{...rows[0],status:"done"}]]
                    .every(value=>{try {api.summarize(value);return false;}catch{return true;}});
                  return {scheduled:summary.scheduled,pending:summary.pending,
                    order:summary.rows.map(r=>r.id),rejected,
                    utc:api.timeLabel(summary.rows[0],"Europe/Moscow"),
                    legacy:api.timeLabel(summary.rows[1],"Europe/Moscow")};
                }"""
            )
            assert result == {
                "scheduled": 1,
                "pending": 1,
                "order": [2, 1],
                "rejected": True,
                "utc": "2099-01-01 13:00 · Europe/Moscow",
                "legacy": "2099-01-01 09:00 · старое время сервера",
            }
        finally:
            browser.close()
