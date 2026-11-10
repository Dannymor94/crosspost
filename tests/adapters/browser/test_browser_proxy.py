"""Tests T-204 (updated): proxy is resolved inside open_page() from channel declaration.

Verifies that every browser path (publish, login, session-validator, CLI-login)
gets the proxy automatically — no caller can bypass it by forgetting a param.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crosspost.adapters.browser import base_browser as bb


class _FakeAsyncPW:
    def __init__(self, pw):
        self._pw = pw

    async def __aenter__(self):
        return self._pw

    async def __aexit__(self, *exc):
        return False


def _fake_pw_for_publish():
    pw = MagicMock()
    browser = AsyncMock()
    context = AsyncMock()
    page = AsyncMock()
    pw.chromium.launch = AsyncMock(return_value=browser)
    browser.new_context = AsyncMock(return_value=context)
    context.new_page = AsyncMock(return_value=page)
    return pw, browser, context, page


def _fake_pw_for_login():
    pw = MagicMock()
    context = AsyncMock()
    context.pages = []
    page = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.storage_state = AsyncMock()
    pw.chromium.launch_persistent_context = AsyncMock(return_value=context)
    return pw, context, page


@pytest.fixture(autouse=True)
def profiles_root(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_PROFILES_DIR", str(tmp_path / "browser_profiles"))
    return tmp_path / "browser_profiles"


# ── publish path ──────────────────────────────────────────────────────────────


async def test_publish_path_gets_proxy_from_env(monkeypatch):
    """open_page('vk_wall') resolves VK_PROXY_URL automatically."""
    monkeypatch.setenv("VK_PROXY_URL", "http://ru-proxy:3128")
    pw, browser, context, page = _fake_pw_for_publish()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.open_page("vk_wall", headless=True, storage_state=None):
            pass

    proxy = browser.new_context.call_args.kwargs.get("proxy")
    assert proxy == {"server": "http://ru-proxy:3128"}


async def test_publish_path_webrtc_disabled_when_proxy(monkeypatch):
    monkeypatch.setenv("VK_PROXY_URL", "http://ru-proxy:3128")
    pw, browser, context, page = _fake_pw_for_publish()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.open_page("vk_wall", headless=True, storage_state=None):
            pass

    args = pw.chromium.launch.call_args.kwargs.get("args", [])
    assert any("disable_non_proxied_udp" in a for a in args)


async def test_publish_path_raises_when_proxy_missing(monkeypatch):
    """open_page for channel with proxy_env_var but env unset → RuntimeError."""
    monkeypatch.delenv("VK_PROXY_URL", raising=False)
    with pytest.raises(RuntimeError, match="VK_PROXY_URL"):
        async with bb.open_page("vk_wall", headless=True, storage_state=None):
            pass


# ── CLI-login path ────────────────────────────────────────────────────────────


async def test_cli_login_path_gets_proxy_from_env(monkeypatch):
    """login_context('yandex') resolves YANDEX_PROXY_URL automatically."""
    monkeypatch.setenv("YANDEX_PROXY_URL", "http://ru-proxy:8080")
    pw, context, page = _fake_pw_for_login()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.login_context("yandex", headless=True):
            pass

    kwargs = pw.chromium.launch_persistent_context.call_args.kwargs
    assert kwargs.get("proxy") == {"server": "http://ru-proxy:8080"}
    assert any("disable_non_proxied_udp" in a for a in kwargs.get("args", []))


async def test_cli_login_path_raises_when_proxy_missing(monkeypatch):
    monkeypatch.delenv("YANDEX_PROXY_URL", raising=False)
    with pytest.raises(RuntimeError, match="YANDEX_PROXY_URL"):
        async with bb.login_context("yandex", headless=True):
            pass


# ── UI-login path (browser_login.begin) ──────────────────────────────────────


async def test_ui_login_path_gets_proxy_from_env(monkeypatch):
    """browser_login.begin() reads proxy_env_var from validator and passes to _launch."""
    from crosspost.channels import browser_login as bl

    monkeypatch.setenv("VK_PROXY_URL", "http://ru-proxy:3128")
    captured: dict = {}

    async def fake_launch(headless=False, proxy_url=""):
        captured["proxy_url"] = proxy_url
        browser = AsyncMock()
        context = AsyncMock()
        context.storage_state = AsyncMock(return_value={})
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://vk.com/"
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)
        return MagicMock(), browser, context, page

    monkeypatch.setattr(bl, "_launch", fake_launch)
    bl._SESSIONS.clear()
    await bl.begin(1, "vk_wall")
    bl._SESSIONS.clear()

    assert captured["proxy_url"] == "http://ru-proxy:3128"


async def test_ui_login_path_empty_proxy_raises(monkeypatch):
    """browser_login.begin() without proxy env var → RuntimeError from _get_proxy."""
    from crosspost.channels import browser_login as bl

    monkeypatch.delenv("VK_PROXY_URL", raising=False)
    captured: dict = {}

    async def fake_launch(headless=False, proxy_url=""):
        captured["called"] = True
        # _launch should not be called if proxy is missing; if it is called with
        # an empty proxy_url, the proxy check inside open_page would catch it.
        # But begin() reads proxy before _launch, so this should not be reached.
        raise AssertionError("_launch called without proxy")

    monkeypatch.setattr(bl, "_launch", fake_launch)
    bl._SESSIONS.clear()

    with pytest.raises(RuntimeError, match="VK_PROXY_URL"):
        await bl.begin(1, "vk_wall")
    bl._SESSIONS.clear()


# ── session-validator path ────────────────────────────────────────────────────


async def test_session_validator_path_gets_proxy_from_env(monkeypatch):
    """_make_browser_validator calls open_page which auto-resolves proxy."""
    monkeypatch.setenv("VK_PROXY_URL", "http://ru-proxy:3128")
    pw, browser, context, page = _fake_pw_for_publish()
    page.url = "https://vk.com/feed"
    page.goto = AsyncMock()

    # Grab the validator function for vk_wall from VALIDATORS.
    from crosspost.channels.validators import VALIDATORS

    v = VALIDATORS["vk_wall"]
    validate_fn = v.fn
    assert validate_fn is not None

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        # Pass a dummy storage_state so open_page doesn't try to read a file.
        await validate_fn('{"cookies":[],"origins":[]}')

    proxy = browser.new_context.call_args.kwargs.get("proxy")
    assert proxy == {"server": "http://ru-proxy:3128"}


# ── proxy with credentials ────────────────────────────────────────────────────


async def test_proxy_with_credentials_split_correctly(monkeypatch):
    """Proxy URL with user:pass → server/username/password split, password not in server."""
    monkeypatch.setenv("VK_PROXY_URL", "http://user:secret@ru-proxy:3128")
    pw, browser, context, page = _fake_pw_for_publish()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.open_page("vk_wall", headless=True, storage_state=None):
            pass

    proxy = browser.new_context.call_args.kwargs.get("proxy")
    assert proxy is not None
    assert proxy["username"] == "user"
    assert proxy["password"] == "secret"
    assert "secret" not in proxy["server"]


async def test_proxy_urlencoded_credentials_decoded(monkeypatch):
    """URL-encoded password (%40 etc.) is decoded before passing to Playwright."""
    monkeypatch.setenv("VK_PROXY_URL", "http://user%40corp:p%40ss%3Aword@ru-proxy:3128")
    pw, browser, context, page = _fake_pw_for_publish()

    with patch("playwright.async_api.async_playwright", MagicMock(return_value=_FakeAsyncPW(pw))):
        async with bb.open_page("vk_wall", headless=True, storage_state=None):
            pass

    proxy = browser.new_context.call_args.kwargs.get("proxy")
    assert proxy is not None
    assert proxy["username"] == "user@corp"
    assert proxy["password"] == "p@ss:word"
