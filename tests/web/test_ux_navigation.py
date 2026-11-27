"""Responsive shared navigation with host-controlled transitions and no real API."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

FRONTEND = Path(__file__).resolve().parents[2] / "src/crosspost/web/frontend"


def test_workspace_navigation_and_accessibility():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(
                '<main class="cp-ux"><div class="cp-shell">'
                '<aside class="cp-sidebar"><div id="desktop"></div></aside>'
                '<div class="cp-main"><header class="cp-topbar">Демо-профиль</header>'
                '<section class="cp-content"><h1 class="cp-title">Обзор</h1>'
                '<div class="cp-panel">Тестовый экран</div></section></div></div>'
                '<div id="mobile"></div></main>'
            )
            page.add_style_tag(path=FRONTEND / "styles/ux-system.css")
            page.add_script_tag(path=FRONTEND / "shared/ux-navigation.js")
            page.evaluate(
                """() => {
                  const items = [
                    {id:"home",label:"Обзор"}, {id:"calendar",label:"Контент-план"},
                    {id:"posts",label:"Публикации"}, {id:"moderation",label:"Модерация"},
                    {id:"clients",label:"Клиенты"}, {id:"connections",label:"Подключения"},
                    {id:"more",label:"Ещё"}
                  ];
                  window.calls = [];
                  const navigate = id => window.calls.push(id);
                  window.desktop = window.CrosspostNavigation.mount(
                    document.querySelector("#desktop"),
                    {items,active:"home",onNavigate:navigate,label:"Меню компьютера"});
                  const mobile = items.filter(x => !["clients","connections"].includes(x.id));
                  window.mobileNav = window.CrosspostNavigation.mount(
                    document.querySelector("#mobile"),
                    {items:mobile,active:"home",onNavigate:navigate,label:"Меню телефона"});
                  const node = document.querySelector("#mobile nav");
                  // The mobile layout is a nav, not an outer decorative container.
                  node.classList.add("cp-mobile-nav");
                }"""
            )
            desktop = page.get_by_role("navigation", name="Меню компьютера")
            mobile = page.get_by_role("navigation", name="Меню телефона")
            expect(desktop).to_be_visible()
            expect(mobile).to_be_hidden()
            home = desktop.get_by_role("button", name="Обзор")
            posts = desktop.get_by_role("button", name="Публикации")
            expect(home).to_have_attribute("aria-current", "page")
            posts.click()
            assert page.evaluate("window.calls") == ["posts"]
            # The host may reject navigation, e.g. because a draft is unsaved.
            expect(home).to_have_attribute("aria-current", "page")
            assert page.evaluate("window.desktop.setActive('posts')") is True
            assert page.evaluate("window.mobileNav.setActive('posts')") is True
            expect(posts).to_have_attribute("aria-current", "page")
            assert page.evaluate("window.desktop.setActive('not-allowed')") is False
            expect(posts).to_have_attribute("aria-current", "page")
            for width in (360, 390, 768, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                visible = mobile if width <= 640 else desktop
                hidden = desktop if width <= 640 else mobile
                expect(visible).to_be_visible()
                expect(hidden).to_be_hidden()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                sizes = visible.get_by_role("button").evaluate_all(
                    "buttons => buttons.map(b => b.getBoundingClientRect().height)"
                )
                assert all(size >= 44 for size in sizes)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(mobile.get_by_role("button", name="Клиенты")).to_have_count(0)
            mobile.get_by_role("button", name="Ещё").click()
            assert page.evaluate("window.calls") == ["posts", "more"]
            page.evaluate("window.mobileNav.dispose()")
            expect(mobile).to_have_count(0)
        finally:
            browser.close()
