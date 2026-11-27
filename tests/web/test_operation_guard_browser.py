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
                        }, ops.publicationExpectation(["telegram"]));
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


def test_incomplete_receipts_keep_operation_proof():
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
                    const first = {channel:"telegram", target:"1", status:"done"};
                    const second = {channel:"telegram", target:"2", status:"done"};
                    const art = {channel:"telegraph", target:"", status:"done"};
                    const expected = ops.publicationExpectation([
                        {channel:"telegram", target_id:"1"},
                        {channel:"telegram", target_id:"2"}], true);
                    const cases = [
                        [first, second], [first, first, art],
                        [first, {...second, target:"3"}, art],
                        [first, {...second, status:"unknown"}, art],
                        [first, second, art],
                    ];
                    const states = [];
                    for (const [i, outcomes] of cases.entries()) {
                        const key = "test:" + i;
                        try {
                            await ops.run(key, async () => ({publication_id:"p", outcomes}), expected);
                        } catch {}
                        states.push(ops.pending(key));
                    }
                    try {
                        await ops.run("schedule", async () => ({id:1,status:"unknown"}),
                            {kind:"scheduled"});
                    } catch {}
                    return {states, schedule:ops.pending("schedule")};
                }"""
            )
            assert result == {"states": [True, True, True, True, False], "schedule": True}
            page.route(
                "**/operation-status",
                lambda route: route.fulfill(
                    json={
                        "state": "recorded",
                        "outcomes": [{"channel": "telegram", "target": "1", "status": "done"}],
                    }
                ),
            )
            result = page.evaluate(
                """async () => {
                    const ops = window.CrosspostOperations;
                    const receipt = await ops.recover("test:0", "/api/profiles/1");
                    await ops.acknowledge("test:0", receipt);
                    return {known:receipt.known, pending:ops.pending("test:0")};
                }"""
            )
            assert result == {"known": False, "pending": True}
        finally:
            browser.close()


def test_editor_bound_proof_survives_known_result_until_acknowledged():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context()
            context.route("http://localhost/**", lambda route: route.fulfill(body="<html></html>"))
            first, second = context.new_page(), context.new_page()
            for page in (first, second):
                page.goto("http://localhost/")
                page.add_script_tag(path=SCRIPT)
            result = first.evaluate(
                """async () => {
                    const ops = window.CrosspostOperations;
                    const draft = {scope:"a".repeat(64), revision:"b".repeat(32)};
                    let headers;
                    await ops.run("draft", async h => {
                        headers = h;
                        return {publication_id:"p",outcomes:[
                            {channel:"telegram",target:"",status:"submitted"}]};
                    },ops.publicationExpectation(["telegram"]),draft);
                    return {headers,pending:ops.pending("draft")};
                }"""
            )
            assert result["pending"] is True
            assert result["headers"]["Idempotency-Key"] == "b" * 32
            assert result["headers"]["X-Draft-Revision"] == "b" * 32
            assert second.evaluate("window.CrosspostOperations.pending('draft')") is True
        finally:
            browser.close()


def test_definitive_rejection_recovery_is_distinct_from_unknown():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.route("http://localhost/**", lambda route: route.fulfill(body="<html></html>"))
            page.goto("http://localhost/")
            page.add_script_tag(path=SCRIPT)
            page.route(
                "**/operation-status",
                lambda route: route.fulfill(
                    json={
                        "state": "rejected",
                        "outcomes": [{"status": "rejected_before_effect", "http_status": "403"}],
                    }
                ),
            )
            result = page.evaluate(
                """async () => {
                    const ops = window.CrosspostOperations;
                    try {
                        await ops.run("rejected", async () => { throw Error("403"); },
                            ops.publicationExpectation(["telegram"]),
                            {scope:"a".repeat(64),revision:"b".repeat(32)});
                    } catch {}
                    const receipt = await ops.recover("rejected", "/api/profiles/1");
                    await ops.acknowledge("rejected", receipt);
                    return {known:receipt.known,rejected:receipt.rejected,
                        pending:ops.pending("rejected")};
                }"""
            )
            assert result == {"known": True, "rejected": True, "pending": False}
        finally:
            browser.close()
