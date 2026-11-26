"""Browser regression for draft result classification and snapshot normalization."""

from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend/editor-draft.js"


def test_draft_confirmation_requires_all_nonempty_done():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.add_script_tag(path=SCRIPT)
            actual = page.evaluate(
                """() => {
                    const d = window.CrosspostDraft;
                    const statuses = [[], [{status:"submitted"}], [{status:"failed"}],
                      [{status:"done"},{status:"submitted"}], [{status:"done"}]];
                    return {results: statuses.map(d.confirmed),
                      targets: d.normalize({targets:["b","a","b"]}).targets};
                }"""
            )
            assert actual == {"results": [False, False, False, False, True], "targets": ["a", "b"]}
        finally:
            browser.close()
