"""Тесты двухшагового браузерного входа на моках Playwright.

Проверяем:
  - begin открывает СВЕЖИЙ ПУСТОЙ контекст на странице входа;
  - confirm ждёт загрузки (_settle) и судит по ФАКТУ (DOM-маркеры), не по URL;
  - страница с формой входа → is_logged_in False → confirm ждёт (None), окно живо;
  - ПУСТАЯ снятая сессия (нет кук домена) → ошибка, НЕ ложный успех;
  - изоляция по (profile, channel); cancel закрывает окно.
"""

from __future__ import annotations

import pytest

from crosspost.channels import browser_login as bl

_VK_STATE = {"cookies": [{"name": "remixsid", "value": "X", "domain": ".vk.com"}], "origins": []}


class _FakeContext:
    def __init__(self, state: dict) -> None:
        self._state = state
        self.closed = False

    async def storage_state(self) -> dict:
        return self._state

    async def close(self) -> None:
        self.closed = True


class _FakePage:
    """Фейк-страница. logged_out — есть ли DOM-маркер НЕзалогиненного."""

    def __init__(self, *, url: str = "", logged_out: bool = False) -> None:
        self.url = url
        self.goto_urls: list[str] = []
        self.logged_out = logged_out

    async def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        self.url = url

    async def wait_for_load_state(self, *a, **k):
        return None

    async def query_selector(self, sel):
        # Маркер «не залогинен» присутствует, только если logged_out=True.
        return object() if self.logged_out else None

    async def wait_for_selector(self, sel, **k):
        return object()


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakePW:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _install_launch(monkeypatch, state: dict, *, logged_out: bool = False):
    """Подменить _launch фейковым браузером. Возвращает (pw, browser, ctx, page)."""
    pw = _FakePW()
    browser = _FakeBrowser()
    ctx = _FakeContext(state)
    page = _FakePage(logged_out=logged_out)

    async def fake_launch(headless=False):
        return pw, browser, ctx, page

    monkeypatch.setattr(bl, "_launch", fake_launch)
    return pw, browser, ctx, page


def _install_is_logged_in(monkeypatch, *, result: bool = True):
    """Подменить is_logged_in константой (когда важна не проверка входа, а флоу)."""
    from crosspost.adapters.browser import base_browser as bb

    async def fake(page, *, reject_url_fragments=(), reject_selectors=(), require_selector=None, timeout=5000):
        return result

    monkeypatch.setattr(bb, "is_logged_in", fake)


@pytest.fixture(autouse=True)
def _clear():
    bl._SESSIONS.clear()
    yield
    bl._SESSIONS.clear()


async def test_begin_opens_empty_context_on_login_page(monkeypatch):
    pw, browser, ctx, page = _install_launch(monkeypatch, {"cookies": []})

    session_key = await bl.begin(1, "vk_wall")

    assert session_key == "vk_wall"
    assert (1, "vk_wall") in bl._SESSIONS
    # Открыли страницу входа (login_url), НЕ probe.
    assert page.goto_urls == ["https://vk.com/"]


async def test_confirm_success_returns_state_and_closes(monkeypatch):
    pw, browser, ctx, page = _install_launch(monkeypatch, _VK_STATE)
    _install_is_logged_in(monkeypatch, result=True)

    await bl.begin(1, "vk_wall")
    result = await bl.confirm(1, "vk_wall")

    # confirm навигировал на ЗАЩИЩЁННУЮ probe-страницу (не судим по login_url).
    assert page.goto_urls[-1] == "https://vk.com/feed"
    assert result == _VK_STATE
    assert (1, "vk_wall") not in bl._SESSIONS
    assert ctx.closed and browser.closed and pw.stopped


async def test_confirm_not_logged_in_returns_none_and_keeps_window(monkeypatch):
    _install_is_logged_in(monkeypatch, result=False)
    _install_launch(monkeypatch, {"cookies": []})

    await bl.begin(1, "vk_wall")
    result = await bl.confirm(1, "vk_wall")

    assert result is None
    # Окно ЖИВО — пользователь может дожать вход.
    assert (1, "vk_wall") in bl._SESSIONS


# ── БАГ: ложный успех на странице входа ──────────────────────────────────────


async def test_login_form_present_blocks_success(monkeypatch):
    """Страница с формой входа (DOM-маркер) → РЕАЛЬНЫЙ is_logged_in False → None.

    Это точный сценарий бага: «ВКонтакте | Добро пожаловать» открыта, но раньше
    is_logged_in судил только по URL и мгновенно давал True.
    """
    # НЕ подменяем is_logged_in — используем настоящий. page.logged_out=True →
    # query_selector найдёт маркер «Войти»/«Добро пожаловать».
    _install_launch(monkeypatch, _VK_STATE, logged_out=True)

    await bl.begin(1, "vk_wall")
    result = await bl.confirm(1, "vk_wall")

    assert result is None  # не залогинен — успех НЕ засчитан
    assert (1, "vk_wall") in bl._SESSIONS  # окно живо


async def test_empty_storage_state_raises_not_success(monkeypatch):
    """Пустая снятая сессия (нет кук домена) → ошибка, НЕ ложный LIVE (страховка #3)."""
    _install_is_logged_in(monkeypatch, result=True)  # даже если проверка входа «прошла»
    _install_launch(monkeypatch, {"cookies": [], "origins": []})

    await bl.begin(1, "vk_wall")

    with pytest.raises(bl.BrowserLoginError, match="сессия пустая"):
        await bl.confirm(1, "vk_wall")


async def test_cookies_of_other_domain_do_not_count(monkeypatch):
    """Куки чужого домена не считаются валидной VK-сессией."""
    _install_is_logged_in(monkeypatch, result=True)
    _install_launch(monkeypatch, {"cookies": [{"name": "x", "domain": ".example.com"}]})

    await bl.begin(1, "vk_wall")
    with pytest.raises(bl.BrowserLoginError, match="сессия пустая"):
        await bl.confirm(1, "vk_wall")


async def test_confirm_without_begin_raises(monkeypatch):
    with pytest.raises(bl.BrowserLoginError, match="Окно входа не открыто"):
        await bl.confirm(7, "vk_wall")


async def test_isolation_two_profiles_independent(monkeypatch):
    _install_is_logged_in(monkeypatch, result=True)

    state_a = {"cookies": [{"name": "A", "domain": ".vk.com"}]}
    state_b = {"cookies": [{"name": "B", "domain": ".vk.com"}]}
    _install_launch(monkeypatch, state_a)
    await bl.begin(1, "vk_wall")
    _install_launch(monkeypatch, state_b)
    await bl.begin(2, "vk_wall")

    assert (1, "vk_wall") in bl._SESSIONS
    assert (2, "vk_wall") in bl._SESSIONS

    rb = await bl.confirm(2, "vk_wall")
    assert rb == state_b
    # Окно A НЕ тронуто закрытием B.
    assert (1, "vk_wall") in bl._SESSIONS
    assert (2, "vk_wall") not in bl._SESSIONS


async def test_cancel_closes_window(monkeypatch):
    pw, browser, ctx, page = _install_launch(monkeypatch, {"cookies": []})
    await bl.begin(1, "vk_wall")

    await bl.cancel(1, "vk_wall")
    assert (1, "vk_wall") not in bl._SESSIONS
    assert ctx.closed and browser.closed and pw.stopped


async def test_begin_rejects_non_browser_channel(monkeypatch):
    with pytest.raises(bl.BrowserLoginError, match="не браузерный"):
        await bl.begin(1, "telegram")


# ── _has_session_cookies (юнит) ──────────────────────────────────────────────


def test_has_session_cookies_matches_domain():
    assert bl._has_session_cookies({"cookies": [{"domain": ".vk.com"}]}, ("vk.com",)) is True
    assert bl._has_session_cookies({"cookies": []}, ("vk.com",)) is False
    assert bl._has_session_cookies({"cookies": [{"domain": "x.ru"}]}, ("vk.com",)) is False
    # Домены не заданы — проверка не требуется.
    assert bl._has_session_cookies({"cookies": []}, ()) is True
