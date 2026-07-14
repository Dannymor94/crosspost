"""Тесты двухшагового браузерного входа на моках Playwright.

Детекция входа — по НАДЁЖНЫМ фактам (URL защищённой страницы + куки аккаунта),
НЕ по тексту DOM. Регресс: на залогиненной ВК слово «Войти» может присутствовать —
это НЕ должно давать ложный «не вошёл».

Проверяем:
  - begin открывает СВЕЖИЙ ПУСТОЙ контекст на странице входа;
  - confirm: залогинен (feed + куки vk.com) → успех; редирект на login → None;
  - пустая сессия (нет кук домена) → None (не ложный успех), окно живо;
  - инцидентный текст «Войти» на залогиненной странице → успех (регресс);
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
    """Фейк-страница.

    land_url — куда «приземляется» после goto (имитация редиректа аноним → login).
    has_login_text — query_selector находит «Войти» (проверяем, что это НЕ ломает вход).
    """

    def __init__(self, *, land_url: str | None = None, has_login_text: bool = False) -> None:
        self._land_url = land_url
        self._has_login_text = has_login_text
        self.url = ""
        self.goto_urls: list[str] = []

    async def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        self.url = self._land_url or url

    async def wait_for_load_state(self, *a, **k):
        return None

    async def query_selector(self, sel):
        return object() if self._has_login_text else None

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


def _install_launch(monkeypatch, state: dict, *, land_url=None, has_login_text=False):
    """Подменить _launch фейковым браузером. Возвращает (pw, browser, ctx, page)."""
    pw = _FakePW()
    browser = _FakeBrowser()
    ctx = _FakeContext(state)
    page = _FakePage(land_url=land_url, has_login_text=has_login_text)

    async def fake_launch(headless=False):
        return pw, browser, ctx, page

    monkeypatch.setattr(bl, "_launch", fake_launch)
    return pw, browser, ctx, page


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
    assert page.goto_urls == ["https://vk.com/"]  # login_url, НЕ probe


async def test_confirm_success_when_on_feed_with_cookies(monkeypatch):
    pw, browser, ctx, page = _install_launch(monkeypatch, _VK_STATE)  # feed, куки vk.com

    await bl.begin(1, "vk_wall")
    result = await bl.confirm(1, "vk_wall")

    assert page.goto_urls[-1] == "https://vk.com/feed"  # confirm пошёл на probe
    assert result == _VK_STATE
    assert (1, "vk_wall") not in bl._SESSIONS
    assert ctx.closed and browser.closed and pw.stopped


async def test_confirm_redirected_to_login_returns_none(monkeypatch):
    # Аноним: probe(feed) редиректит на login → не вошёл.
    _install_launch(monkeypatch, _VK_STATE, land_url="https://vk.com/login")

    await bl.begin(1, "vk_wall")
    result = await bl.confirm(1, "vk_wall")

    assert result is None
    assert (1, "vk_wall") in bl._SESSIONS  # окно живо


# ── РЕГРЕСС: инцидентный текст «Войти» не должен ломать вход ───────────────────


async def test_incidental_login_text_does_not_false_negative(monkeypatch):
    """Залогинен (feed + куки), но на странице ЕСТЬ «Войти» (футер/виджет) → УСПЕХ.

    Раньше logged_out_selectors ловили любое «Войти» → ложный «не вошёл». Теперь
    детекция по URL+кукам, текст «Войти» игнорируется.
    """
    _install_launch(monkeypatch, _VK_STATE, has_login_text=True)

    await bl.begin(1, "vk_wall")
    result = await bl.confirm(1, "vk_wall")

    assert result == _VK_STATE  # вход засчитан несмотря на текст «Войти»
    assert (1, "vk_wall") not in bl._SESSIONS


# ── Страховка от пустой сессии ────────────────────────────────────────────────


async def test_empty_storage_state_returns_none(monkeypatch):
    """feed открыт, но storageState пуст (нет кук домена) → None, НЕ ложный успех."""
    _install_launch(monkeypatch, {"cookies": [], "origins": []})

    await bl.begin(1, "vk_wall")
    result = await bl.confirm(1, "vk_wall")

    assert result is None
    assert (1, "vk_wall") in bl._SESSIONS  # окно живо


async def test_cookies_of_other_domain_do_not_count(monkeypatch):
    _install_launch(monkeypatch, {"cookies": [{"name": "x", "domain": ".example.com"}]})

    await bl.begin(1, "vk_wall")
    assert await bl.confirm(1, "vk_wall") is None


async def test_confirm_without_begin_raises(monkeypatch):
    with pytest.raises(bl.BrowserLoginError, match="Окно входа не открыто"):
        await bl.confirm(7, "vk_wall")


async def test_isolation_two_profiles_independent(monkeypatch):
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
    assert (1, "vk_wall") in bl._SESSIONS  # окно A не тронуто
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


# ── Диагностика / хелперы (юнит) ──────────────────────────────────────────────


def test_has_session_cookies_matches_domain():
    assert bl._has_session_cookies({"cookies": [{"domain": ".vk.com"}]}, ("vk.com",)) is True
    assert bl._has_session_cookies({"cookies": []}, ("vk.com",)) is False
    assert bl._has_session_cookies({"cookies": [{"domain": "x.ru"}]}, ("vk.com",)) is False
    assert bl._has_session_cookies({"cookies": []}, ()) is True


def test_login_failure_reason_diagnoses():
    from crosspost.channels.validators import VALIDATORS

    v = VALIDATORS["vk_wall"]
    # Редирект на login → причина про URL.
    r = bl._login_failure_reason("https://vk.com/login", _VK_STATE, v)
    assert r is not None and "страница входа" in r
    # Пустые куки → причина про сессию, с перечислением имён кук.
    r2 = bl._login_failure_reason("https://vk.com/feed", {"cookies": []}, v)
    assert r2 is not None and "сессия пустая" in r2
    # Залогинен (feed + куки vk.com) → None.
    assert bl._login_failure_reason("https://vk.com/feed", _VK_STATE, v) is None
