"""Shared editor preview is presentation-only and treats text as text."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

FRONTEND = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend"


def test_preview_is_approximate_and_does_not_render_markup():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content('<details id="preview" open></details>')
            page.add_script_tag(path=FRONTEND / "shared/ux-editor-preview.js")
            page.evaluate(
                """() => window.CrosspostEditorPreview.mount(document.querySelector("#preview"), {
                  text:'<img src=x onerror=alert(1)>',media:"missing",targetState:"ready",
                  targets:[{title:"Telegram"}]
                })"""
            )
            expect(page.get_by_text("Предпросмотр публикации")).to_be_visible()
            expect(page.get_by_text("<img src=x onerror=alert(1)>", exact=True)).to_be_visible()
            expect(page.locator("img")).to_have_count(0)
            expect(page.get_by_text("Выбрано: Telegram")).to_be_visible()
            expect(page.get_by_text("Нужно выбрать вложение заново")).to_be_visible()
            page.evaluate(
                """() => window.CrosspostEditorPreview.mount(document.querySelector("#preview"), {
                  targetState:"error",targets:[{title:"Telegram"}]
                })"""
            )
            expect(page.get_by_text("Цели ещё не подтверждены")).to_be_visible()
            expect(page.get_by_text("Выбрано: Telegram")).to_have_count(0)
        finally:
            browser.close()
