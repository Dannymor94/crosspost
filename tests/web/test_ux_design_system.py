"""Shared UX primitives: real Chromium checks, no network or publication API."""

from pathlib import Path

from playwright.sync_api import sync_playwright

FRONTEND = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend"


def test_design_tokens_and_theme_controller():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844}, color_scheme="dark")
            page.route("**/*", lambda route: route.fulfill(body="<html></html>"))
            page.goto("http://localhost/")
            page.set_content(
                '<main class="cp-ux" id="ux"><section class="cp-panel cp-stack">'
                '<button class="cp-button cp-button--primary">Создать пост</button>'
                '<input class="cp-input" aria-label="Текст поста">'
                '<p class="cp-status--warning">Требует внимания</p>'
                "</section></main>"
            )
            page.add_style_tag(path=FRONTEND / "styles/ux-system.css")
            page.add_script_tag(path=FRONTEND / "shared/ux-theme.js")
            actual = page.evaluate(
                """() => {
                    const root = document.querySelector("#ux");
                    const theme = window.CrosspostTheme.mount(root);
                    window.testTheme = theme;
                    const button = root.querySelector("button");
                    const input = root.querySelector("input");
                    const contrast = (a,b) => {
                      const luminance = rgb => {
                        const vals = rgb.match(/\\d+/g).map(Number).map(v => {
                          const n = v / 255;
                          return n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4;
                        });
                        return .2126*vals[0]+.7152*vals[1]+.0722*vals[2];
                      };
                      const x = luminance(a), y = luminance(b);
                      return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);
                    };
                    const css = getComputedStyle(root);
                    return {theme:root.dataset.theme, preference:theme.preference,
                      background:css.backgroundColor, textContrast:contrast(css.color,css.backgroundColor),
                      buttonContrast:contrast(getComputedStyle(button).color,
                        getComputedStyle(button).backgroundColor),
                      buttonHeight:button.getBoundingClientRect().height,
                      inputHeight:input.getBoundingClientRect().height};
                }"""
            )
            assert actual["theme"] == "dark"
            assert actual["preference"] == "system"
            assert actual["background"] == "rgb(16, 20, 28)"
            assert actual["textContrast"] >= 4.5
            assert actual["buttonContrast"] >= 4.5
            assert actual["buttonHeight"] >= 44 and actual["inputHeight"] >= 44
            page.evaluate("window.testTheme.set('light')")
            assert page.locator("#ux").get_attribute("data-theme") == "light"
            page.evaluate("window.testTheme.dispose()")
            page.evaluate(
                "window.testTheme = window.CrosspostTheme.mount(document.querySelector('#ux'))"
            )
            assert page.locator("#ux").get_attribute("data-theme") == "light"
            page.evaluate("window.testTheme.set('system')")
            page.emulate_media(color_scheme="light")
            assert page.locator("#ux").get_attribute("data-theme") == "light"
            page.emulate_media(color_scheme="dark")
            assert page.locator("#ux").get_attribute("data-theme") == "dark"
            page.evaluate(
                """() => {
                  window.testTheme.dispose();
                  window.testTheme = window.CrosspostTheme.mount(document.querySelector("#ux"), {
                    storage: {getItem() {throw Error("denied")}, setItem() {throw Error("quota")}}
                  });
                  window.testTheme.set("light");
                }"""
            )
            assert page.locator("#ux").get_attribute("data-theme") == "light"
            page.keyboard.press("Tab")
            assert page.locator("button").evaluate("e => getComputedStyle(e).outlineStyle") == "solid"
        finally:
            browser.close()
