"""Shared editor target selection and stale-response fencing."""

from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend/editor-targets.js"


def test_editor_target_intersection_and_stale_response():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.add_script_tag(path=SCRIPT)
            result = page.evaluate(
                """() => {
                    const tools = window.CrosspostEditorTargets;
                    const targets = [
                        {id: "a", eligible: true},
                        {id: "b", eligible: false},
                        {id: "new", eligible: true}
                    ];
                    const result = tools.intersection(["a", "b"], targets, t => t.id);
                    const empty = tools.intersection([], targets, t => t.id);
                    const fence = tools.latest();
                    const old = fence.begin();
                    const next = fence.begin();
                    const latest = next();
                    fence.invalidate();
                    return {result, empty, old: old(), latest, after: next()};
                }"""
            )
            assert result == {
                "result": {"retained": ["a"], "removed": ["b"]},
                "empty": {"retained": [], "removed": []},
                "old": False,
                "latest": True,
                "after": False,
            }
        finally:
            browser.close()
