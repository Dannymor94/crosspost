"""Tests for FastAPI admin panel. Tests/web/test_api.py."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.channels import telegram_login as tl
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ConnectionState
from crosspost.web import deps
from crosspost.web.routes import channels, profiles, tg_login

# ── Test app fixture ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def vault_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VAULT_KEY", key)


@pytest.fixture()
async def test_app() -> AsyncGenerator[FastAPI, None]:
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(factory)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        yield

    app = FastAPI(lifespan=_lifespan)
    app.include_router(profiles.router)
    app.include_router(channels.router)
    app.include_router(tg_login.router)

    yield app

    await engine.dispose()
    tl._LOGIN_SESSIONS.clear()
    from crosspost.channels import browser_login as bl

    bl._SESSIONS.clear()


# ── Мок Telethon-клиента для интерактивного входа ─────────────────────────────


class _FakeTgSession:
    def __init__(self, saved: str) -> None:
        self._saved = saved

    def save(self) -> str:
        return self._saved


class FakeTgClient:
    def __init__(self, saved_session: str = "STRING_SESSION_XYZ") -> None:
        self.session = _FakeTgSession(saved_session)

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def send_code_request(self, phone: str):
        class _Sent:
            phone_code_hash = "HASH"

        return _Sent()

    async def sign_in(self, **kwargs):
        return object()


async def _login_telegram(
    client: AsyncClient, pid: int, *, saved_session: str = "STRING_SESSION_XYZ"
) -> None:
    """Прогнать полный интерактивный вход Telegram (без 2FA) на мок-клиенте."""
    with patch.object(tl, "_build_client", lambda api_id, api_hash: FakeTgClient(saved_session)):
        await client.post(
            f"/api/profiles/{pid}/channels/telegram/login/begin",
            json={"api_id": 42, "api_hash": "hash", "target_channel": "@ch"},
        )
        await client.post(
            f"/api/profiles/{pid}/channels/telegram/login/send-code",
            json={"phone": "+70000000000"},
        )
        await client.post(
            f"/api/profiles/{pid}/channels/telegram/login/sign-in",
            json={"code": "12345"},
        )


@pytest.fixture()
async def client(test_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
        yield c


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_create_and_list_profiles(client: AsyncClient) -> None:
    resp = await client.post("/api/profiles", json={"name": "alice"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "alice"
    assert "id" in data

    resp2 = await client.get("/api/profiles")
    assert resp2.status_code == 200
    names = [p["name"] for p in resp2.json()]
    assert "alice" in names


async def test_fresh_profile_channels_not_connected(client: AsyncClient) -> None:
    resp = await client.post("/api/profiles", json={"name": "bob"})
    pid = resp.json()["id"]

    resp2 = await client.get(f"/api/profiles/{pid}/channels")
    assert resp2.status_code == 200
    statuses = resp2.json()
    assert len(statuses) > 0
    for ch in statuses:
        assert ch["state"] == "not_connected"
        assert ch["has_credential"] is False


async def test_telegram_interactive_login_live(client: AsyncClient) -> None:
    resp = await client.post("/api/profiles", json={"name": "carol"})
    pid = resp.json()["id"]

    await _login_telegram(client, pid)

    # Credential stored, connection live.
    resp3 = await client.get(f"/api/profiles/{pid}/channels")
    tg = next(ch for ch in resp3.json() if ch["channel"] == "telegram")
    assert tg["has_credential"] is True
    assert tg["state"] == "live"
    assert tg["interactive"] is True


class ProgrammableTgClient(FakeTgClient):
    """FakeTgClient с очередью эффектов на sign_in (для 2FA/ошибок)."""

    def __init__(self, effects: list, saved_session: str = "S") -> None:
        super().__init__(saved_session)
        self._effects = list(effects)

    async def sign_in(self, **kwargs):
        effect = self._effects.pop(0)
        if effect is not None:
            raise effect
        return object()


async def test_telegram_2fa_flow_api(client: AsyncClient) -> None:
    from telethon.errors import SessionPasswordNeededError

    resp = await client.post("/api/profiles", json={"name": "olga"})
    pid = resp.json()["id"]

    tg_client = ProgrammableTgClient(
        effects=[SessionPasswordNeededError(request=None), None], saved_session="S2FA"
    )
    with patch.object(tl, "_build_client", lambda api_id, api_hash: tg_client):
        await client.post(
            f"/api/profiles/{pid}/channels/telegram/login/begin",
            json={"api_id": 1, "api_hash": "h", "target_channel": "@c"},
        )
        await client.post(
            f"/api/profiles/{pid}/channels/telegram/login/send-code",
            json={"phone": "+711"},
        )
        r_code = await client.post(
            f"/api/profiles/{pid}/channels/telegram/login/sign-in",
            json={"code": "12345"},
        )
        assert r_code.json()["step"] == "password"

        r_pw = await client.post(
            f"/api/profiles/{pid}/channels/telegram/login/sign-in",
            json={"password": "cloud"},
        )
    assert r_pw.json()["step"] == "done"
    assert r_pw.json()["state"] == "live"


async def test_telegram_invalid_code_api(client: AsyncClient) -> None:
    from telethon.errors import PhoneCodeInvalidError

    resp = await client.post("/api/profiles", json={"name": "pavel"})
    pid = resp.json()["id"]

    tg_client = ProgrammableTgClient(effects=[PhoneCodeInvalidError(request=None)])
    with patch.object(tl, "_build_client", lambda api_id, api_hash: tg_client):
        await client.post(
            f"/api/profiles/{pid}/channels/telegram/login/begin",
            json={"api_id": 1, "api_hash": "h", "target_channel": "@c"},
        )
        await client.post(
            f"/api/profiles/{pid}/channels/telegram/login/send-code",
            json={"phone": "+711"},
        )
        r = await client.post(
            f"/api/profiles/{pid}/channels/telegram/login/sign-in",
            json={"code": "00000"},
        )
    assert r.status_code == 400
    assert "Неверный код" in r.json()["detail"]


# ── Браузерный логин: per-profile сессия в credentials, изоляция ──────────────


def _patch_browser_login(state_dict: dict, *, logged_in: bool = True):
    """Контекст-менеджер: мок Playwright для browser_login + валидации.

    _launch → фейковый браузер, storage_state() отдаёт state_dict.
    is_logged_in → по URL страницы (probe/validate). logged_in=False имитирует
    «пользователь ещё не вошёл» (probe остаётся на странице логина).
    """
    from crosspost.adapters.browser import base_browser as bb
    from crosspost.channels import browser_login as bl

    async def fake_launch(headless=False):
        pw, browser, context, page = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
        context.storage_state = AsyncMock(return_value=state_dict)
        # После confirm probe навигирует сюда; URL решает, «вошёл» ли пользователь.
        page.url = "https://vk.com/feed" if logged_in else "https://vk.com/login"
        return pw, browser, context, page

    @asynccontextmanager
    async def fake_open_page(ch, *, headless=False, session_channel=None, storage_state=None):
        page = AsyncMock()
        page.url = "https://vk.com/feed" if storage_state else "https://vk.com/login"
        yield page

    async def fake_is_logged_in(
        page, *, reject_url_fragments=(), reject_selectors=(), require_selector=None, timeout=5000
    ):
        return not any(f in page.url for f in reject_url_fragments)

    return (
        patch.object(bl, "_launch", fake_launch),
        patch.object(bb, "open_page", fake_open_page),
        patch.object(bb, "is_logged_in", fake_is_logged_in),
    )


async def _browser_login(client: AsyncClient, pid: int, channel: str, state_dict: dict) -> None:
    """Прогнать двухшаговый браузерный вход (begin → confirm) с мок-браузером."""
    # Куки должны иметь домен vk.com — иначе страховка confirm отвергнет сессию.
    for c in state_dict.get("cookies", []):
        c.setdefault("domain", ".vk.com")
    base = f"/api/profiles/{pid}/channels/{channel}/browser-login"
    p_launch, p_open, p_logged = _patch_browser_login(state_dict)
    with p_launch, p_open, p_logged:
        rb = await client.post(f"{base}/begin")
        assert rb.status_code == 200, rb.text
        rc = await client.post(f"{base}/confirm")
        assert rc.status_code == 200, rc.text


async def test_browser_login_stores_per_profile_credential(client: AsyncClient) -> None:
    r = await client.post("/api/profiles", json={"name": "vova"})
    pid = r.json()["id"]

    await _browser_login(client, pid, "vk_wall", {"cookies": [{"name": "sidV"}], "origins": []})

    resp = await client.get(f"/api/profiles/{pid}/channels")
    vk = next(ch for ch in resp.json() if ch["channel"] == "vk_wall")
    assert vk["has_credential"] is True
    assert vk["state"] == "live"


async def test_browser_login_profile_B_does_not_touch_A(client: AsyncClient) -> None:
    ra = await client.post("/api/profiles", json={"name": "profA"})
    rb = await client.post("/api/profiles", json={"name": "profB"})
    pa, pb = ra.json()["id"], rb.json()["id"]

    # A логинится в vk_wall.
    await _browser_login(client, pa, "vk_wall", {"cookies": [{"name": "A"}], "origins": []})

    # B пока БЕЗ сессии — не должен видеть vk_wall как live.
    resp_b = await client.get(f"/api/profiles/{pb}/channels")
    vk_b = next(ch for ch in resp_b.json() if ch["channel"] == "vk_wall")
    assert vk_b["has_credential"] is False
    assert vk_b["state"] == "not_connected"

    # B логинится своей сессией.
    await _browser_login(client, pb, "vk_wall", {"cookies": [{"name": "B"}], "origins": []})

    # У A сессия осталась своя (B её не перезаписал) — оба подключены раздельно.
    resp_a = await client.get(f"/api/profiles/{pa}/channels")
    vk_a = next(ch for ch in resp_a.json() if ch["channel"] == "vk_wall")
    assert vk_a["has_credential"] is True


async def test_disconnect_telegram_resets_login(client: AsyncClient) -> None:
    r = await client.post("/api/profiles", json={"name": "petya"})
    pid = r.json()["id"]

    await _login_telegram(client, pid)
    # Убедились, что подключён.
    before = (await client.get(f"/api/profiles/{pid}/channels")).json()
    tg = next(ch for ch in before if ch["channel"] == "telegram")
    assert tg["state"] == "live" and tg["has_credential"] is True

    resp = await client.post(f"/api/profiles/{pid}/channels/telegram/disconnect")
    assert resp.status_code == 200
    assert resp.json()["state"] == "not_connected"

    after = (await client.get(f"/api/profiles/{pid}/channels")).json()
    tg2 = next(ch for ch in after if ch["channel"] == "telegram")
    assert tg2["state"] == "not_connected"
    assert tg2["has_credential"] is False


async def test_disconnect_vk_wall_clears_shared_vk_session(client: AsyncClient) -> None:
    r = await client.post("/api/profiles", json={"name": "galya"})
    pid = r.json()["id"]

    await _browser_login(client, pid, "vk_wall", {"cookies": [{"name": "S"}], "origins": []})
    # vk_channel делит сессию vk_wall — тоже должен читаться как live.
    chans = (await client.get(f"/api/profiles/{pid}/channels")).json()
    assert next(c for c in chans if c["channel"] == "vk_wall")["has_credential"] is True

    resp = await client.post(f"/api/profiles/{pid}/channels/vk_wall/disconnect")
    assert resp.status_code == 200

    after = (await client.get(f"/api/profiles/{pid}/channels")).json()
    for cid in ("vk_wall", "vk_channel"):
        c = next(ch for ch in after if ch["channel"] == cid)
        assert c["state"] == "not_connected", cid
        assert c["has_credential"] is False, cid


async def test_disconnect_isolated_to_profile(client: AsyncClient) -> None:
    ra = await client.post("/api/profiles", json={"name": "iso_a"})
    rb = await client.post("/api/profiles", json={"name": "iso_b"})
    pa, pb = ra.json()["id"], rb.json()["id"]

    await _browser_login(client, pa, "vk_wall", {"cookies": [{"name": "A"}], "origins": []})
    await _browser_login(client, pb, "vk_wall", {"cookies": [{"name": "B"}], "origins": []})

    # Сброс у A не трогает B.
    await client.post(f"/api/profiles/{pa}/channels/vk_wall/disconnect")

    b_chans = (await client.get(f"/api/profiles/{pb}/channels")).json()
    vk_b = next(c for c in b_chans if c["channel"] == "vk_wall")
    assert vk_b["has_credential"] is True


async def test_browser_login_confirm_not_logged_in_returns_409(client: AsyncClient) -> None:
    """Пользователь нажал «Я вошёл», не войдя: probe на странице логина → 409, не live."""
    r = await client.post("/api/profiles", json={"name": "nikita"})
    pid = r.json()["id"]
    base = f"/api/profiles/{pid}/channels/vk_wall/browser-login"

    p_launch, p_open, p_logged = _patch_browser_login({"cookies": []}, logged_in=False)
    with p_launch, p_open, p_logged:
        assert (await client.post(f"{base}/begin")).status_code == 200
        rc = await client.post(f"{base}/confirm")
    assert rc.status_code == 409

    # Канал НЕ подключился — никакой «автологин» без реального входа.
    resp = await client.get(f"/api/profiles/{pid}/channels")
    vk = next(ch for ch in resp.json() if ch["channel"] == "vk_wall")
    assert vk["has_credential"] is False
    assert vk["state"] == "not_connected"


async def test_browser_login_confirm_without_begin_400(client: AsyncClient) -> None:
    r = await client.post("/api/profiles", json={"name": "sveta"})
    pid = r.json()["id"]
    resp = await client.post(f"/api/profiles/{pid}/channels/vk_wall/browser-login/confirm")
    assert resp.status_code == 400


async def test_connect_rejects_interactive_channel(client: AsyncClient) -> None:
    resp = await client.post("/api/profiles", json={"name": "ivan"})
    pid = resp.json()["id"]
    resp2 = await client.post(
        f"/api/profiles/{pid}/channels/telegram/connect",
        json={"fields": {"session_string": "x"}},
    )
    assert resp2.status_code == 400


async def test_validate_channel_calls_validate_connection(client: AsyncClient) -> None:
    resp = await client.post("/api/profiles", json={"name": "dave"})
    pid = resp.json()["id"]

    with patch(
        "crosspost.web.routes.channels.validate_connection",
        new=AsyncMock(return_value=ConnectionState.NEEDS_RELOGIN),
    ) as mock_vc:
        resp2 = await client.post(f"/api/profiles/{pid}/channels/telegram/validate")

    assert resp2.status_code == 200
    assert resp2.json()["state"] == "needs_relogin"
    mock_vc.assert_awaited_once()


async def test_profile_isolation(client: AsyncClient) -> None:
    r1 = await client.post("/api/profiles", json={"name": "eve"})
    pid1 = r1.json()["id"]
    r2 = await client.post("/api/profiles", json={"name": "frank"})
    pid2 = r2.json()["id"]

    # Connect telegram on profile 1 only
    await _login_telegram(client, pid1, saved_session="s1")

    # Profile 2 should still show not_connected / no credential
    resp = await client.get(f"/api/profiles/{pid2}/channels")
    tg2 = next(ch for ch in resp.json() if ch["channel"] == "telegram")
    assert tg2["state"] == "not_connected"
    assert tg2["has_credential"] is False


async def test_credential_never_in_response(client: AsyncClient) -> None:
    resp = await client.post("/api/profiles", json={"name": "grace"})
    pid = resp.json()["id"]

    await _login_telegram(client, pid, saved_session="super_secret_token")

    # StringSession никогда не возвращается наружу.
    resp3 = await client.get(f"/api/profiles/{pid}/channels")
    assert "super_secret_token" not in resp3.text
