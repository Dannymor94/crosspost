"""Connection status presentation is conservative and does not expose credentials."""

from pathlib import Path

from playwright.sync_api import sync_playwright

FRONTEND = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend"


def test_connection_status_projection_distinguishes_ban_and_unknown():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.add_script_tag(path=FRONTEND / "shared/ux-connections.js")
            result = page.evaluate(
                """() => {
                  const api=window.CrosspostConnections;
                  const rows=api.rows([{channel:"telegram",title:"Telegram",state:"banned",
                    token:"secret",guide:{steps:[{title:"Вход",fields:[{secret:true}]}]}}]);
                  return {row:rows[0],banned:api.describe("banned").label,
                    unknown:api.describe("unexpected").label,
                    manages:["live","needs_relogin","not_connected","banned","unexpected"]
                      .map(api.canManage)};
                }"""
            )
            assert result == {
                "row": {
                    "channel": "telegram",
                    "title": "Telegram",
                    "state": "banned",
                    "expiresSoon": False,
                    "steps": ["Вход"],
                },
                "banned": "Ограничено площадкой",
                "unknown": "Состояние неизвестно",
                "manages": [True, True, True, False, False],
            }
        finally:
            browser.close()
