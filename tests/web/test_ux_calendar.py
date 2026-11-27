"""Calendar projection: civil date arithmetic, explicit zone and unplaced legacy."""

from pathlib import Path

from playwright.sync_api import sync_playwright

FRONTEND = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend"


def test_calendar_projection_respects_profile_day_and_legacy_boundary():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.add_script_tag(path=FRONTEND / "schedule-time.js")
            page.add_script_tag(path=FRONTEND / "shared/ux-calendar.js")
            result = page.evaluate(
                """() => {
                  const cal=window.CrosspostCalendar;
                  const rows=[
                    {id:1,status:"scheduled",basis:"utc",when:"2027-03-28T22:30:00Z"},
                    {id:2,status:"pending_approval",basis:"utc",when:"2027-03-28T01:30:00Z"},
                    {id:3,status:"scheduled",basis:"legacy_local",when:"2027-03-28T12:00:00"},
                    {id:4,status:"scheduled",basis:"utc",when:"2027-03-28T12:00:00"}
                  ];
                  const args={day:"2027-03-28",mode:"week",zone:"Europe/Berlin"};
                  const week=cal.project(rows,args);
                  const only=cal.project(rows,{...args,status:"pending_approval"});
                  const unknown=cal.project(rows,{...args,zone:null});
                  return {week:week.bounds,groups:week.days.map(x=>[x.day,x.rows.map(r=>r.id)]),
                    legacy:week.unplaced.map(r=>r.id),filtered:only.days[0].rows.map(r=>r.id),
                    noZone:unknown.unplaced.length,next:cal.shift(args.day,"week",1),
                    month:cal.period("2028-02-29","month"),
                    december:cal.shift("2027-12-31","month",1)};
                }"""
            )
            assert result == {
                "week": {"start": "2027-03-22", "end": "2027-03-29"},
                "groups": [["2027-03-28", [2]]],
                "legacy": [3, 4],
                "filtered": [2],
                "noZone": 4,
                "next": "2027-04-04",
                "month": {"start": "2028-02-01", "end": "2028-03-01"},
                "december": "2028-01-01",
            }
        finally:
            browser.close()
