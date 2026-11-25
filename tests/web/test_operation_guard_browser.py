"""Browser checks of the shared operation guard without external requests."""

from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend/operation-guard.js"


def test_unknown_operation_survives_reload_and_blocks_send():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.route("http://localhost/**", lambda route: route.fulfill(body="<html></html>"))
            page.goto("http://localhost/")
            page.add_script_tag(path=SCRIPT)
            result = page.evaluate(
                """async () => {
                    const ops = window.CrosspostOperations;
                    const key = ops.scope({id: 1, login: "demo"}, 1);
                    let calls = 0;
                    try {
                        await ops.run(key, async headers => {
                            calls++;
                            if (!headers["Idempotency-Recovery"]) throw Error("no proof");
                            throw Error("lost response");
                        });
                    } catch {}
                    try { await ops.run(key, async () => { calls++; }); } catch {}
                    return {calls, pending: ops.pending(key)};
                }"""
            )
            assert result == {"calls": 1, "pending": True}
            page.reload()
            page.add_script_tag(path=SCRIPT)
            assert page.evaluate(
                """() => {
                    const ops = window.CrosspostOperations;
                    return ops.pending(ops.scope({id: 1, login: "demo"}, 1));
                }"""
            )
            page.route(
                "**/operation-status",
                lambda route: route.fulfill(json={"state": "unknown", "outcomes": []}),
            )
            result = page.evaluate(
                """async () => {
                    const ops = window.CrosspostOperations;
                    const key = ops.scope({id: 1, login: "demo"}, 1);
                    const receipt = await ops.recover(key, "/api/profiles/1");
                    await ops.acknowledge(key, receipt);
                    return {known: receipt.known, pending: ops.pending(key)};
                }"""
            )
            assert result == {"known": False, "pending": True}
            page.unroute("**/operation-status")
            page.route(
                "**/operation-status",
                lambda route: route.fulfill(
                    json={
                        "state": "recorded",
                        "outcomes": [{"channel": "telegram", "target": "", "status": "submitted"}],
                    }
                ),
            )
            result = page.evaluate(
                """async () => {
                    const ops = window.CrosspostOperations;
                    const key = ops.scope({id: 1, login: "demo"}, 1);
                    const receipt = await ops.recover(key, "/api/profiles/1");
                    await ops.acknowledge(key, receipt);
                    return {known: receipt.known, message: receipt.message,
                            pending: ops.pending(key)};
                }"""
            )
            assert result["known"] is True
            assert "без подтверждения" in result["message"]
            assert result["pending"] is False
        finally:
            browser.close()
