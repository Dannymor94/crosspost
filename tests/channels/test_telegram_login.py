"""Тесты интерактивного входа в Telegram на моках Telethon.

Проверяем: happy-path (код без 2FA), ветка 2FA (пароль), человеческие ошибки
(неверный код / неверный пароль), серверное состояние флоу, что телефон/пароль
не утекают после успеха, изоляция по profile_id.
"""

from __future__ import annotations

import pytest
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from crosspost.channels import telegram_login as tl

# ── Мок клиента Telethon ──────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self, saved: str) -> None:
        self._saved = saved

    def save(self) -> str:
        return self._saved


class FakeClient:
    """Мок TelegramClient: программируемое поведение sign_in.

    sign_in_effects — очередь эффектов на последовательные вызовы sign_in:
      None                       → успех
      исключение (класс/инстанс)  → поднять
    """

    def __init__(
        self,
        *,
        saved_session: str = "STRING_SESSION_XYZ",
        sign_in_effects: list | None = None,
    ) -> None:
        self.session = _FakeSession(saved_session)
        self._sign_in_effects = list(sign_in_effects or [None])
        self.connected = False
        self.disconnected = False
        self.sent_phones: list[str] = []
        self.sign_in_calls: list[dict] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send_code_request(self, phone: str):
        self.sent_phones.append(phone)

        class _Sent:
            phone_code_hash = "HASH123"

        return _Sent()

    async def sign_in(self, **kwargs):
        self.sign_in_calls.append(kwargs)
        effect = self._sign_in_effects.pop(0)
        if effect is not None:
            raise effect
        return object()  # "user"


@pytest.fixture(autouse=True)
def _clear_sessions():
    tl._LOGIN_SESSIONS.clear()
    yield
    tl._LOGIN_SESSIONS.clear()


def _install(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    monkeypatch.setattr(tl, "_build_client", lambda api_id, api_hash: client)


# ── Happy path (без 2FA) ──────────────────────────────────────────────────────


async def test_happy_path_no_2fa(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(saved_session="SESS_OK", sign_in_effects=[None])
    _install(monkeypatch, client)

    step = await tl.begin(1, api_id=42, api_hash="hash", target_channel="@ch")
    assert step == "phone"
    assert client.connected

    step = await tl.request_code(1, "+70000000000")
    assert step == "code"
    assert client.sent_phones == ["+70000000000"]

    result = await tl.submit_code(1, code="12345")
    assert result.step == "done"
    assert result.session_string == "SESS_OK"
    assert result.api_id == 42
    assert result.target_channel == "@ch"
    # Флоу очищен, клиент закрыт.
    assert 1 not in tl._LOGIN_SESSIONS
    assert client.disconnected


# ── Ветка 2FA ─────────────────────────────────────────────────────────────────


async def test_2fa_requires_password(monkeypatch: pytest.MonkeyPatch) -> None:
    # Первый sign_in (код) → нужен пароль; второй (пароль) → успех.
    client = FakeClient(
        saved_session="SESS_2FA",
        sign_in_effects=[SessionPasswordNeededError(request=None), None],
    )
    _install(monkeypatch, client)

    await tl.begin(1, api_id=42, api_hash="hash", target_channel="@ch")
    await tl.request_code(1, "+70000000000")

    # Код принят, но требуется облачный пароль.
    result = await tl.submit_code(1, code="12345")
    assert result.step == "password"
    assert result.session_string is None
    assert tl._LOGIN_SESSIONS[1].awaiting_password is True

    # Теперь передаём пароль (без кода).
    result = await tl.submit_code(1, password="cloud_pw")
    assert result.step == "done"
    assert result.session_string == "SESS_2FA"
    assert 1 not in tl._LOGIN_SESSIONS


async def test_2fa_code_and_password_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(
        sign_in_effects=[SessionPasswordNeededError(request=None), None],
    )
    _install(monkeypatch, client)

    await tl.begin(1, api_id=42, api_hash="hash", target_channel="@ch")
    await tl.request_code(1, "+70000000000")

    result = await tl.submit_code(1, code="12345", password="cloud_pw")
    assert result.step == "done"


# ── Человеческие ошибки ───────────────────────────────────────────────────────


async def test_invalid_code_message(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(sign_in_effects=[PhoneCodeInvalidError(request=None)])
    _install(monkeypatch, client)

    await tl.begin(1, api_id=42, api_hash="hash", target_channel="@ch")
    await tl.request_code(1, "+70000000000")

    with pytest.raises(tl.TelegramLoginError, match="Неверный код"):
        await tl.submit_code(1, code="00000")
    # Флоу остаётся живым — пользователь может повторить ввод кода.
    assert 1 in tl._LOGIN_SESSIONS


async def test_invalid_2fa_password_message(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(
        sign_in_effects=[
            SessionPasswordNeededError(request=None),
            PasswordHashInvalidError(request=None),
        ],
    )
    _install(monkeypatch, client)

    await tl.begin(1, api_id=42, api_hash="hash", target_channel="@ch")
    await tl.request_code(1, "+70000000000")
    await tl.submit_code(1, code="12345")  # → step=password

    with pytest.raises(tl.TelegramLoginError, match="Неверный облачный пароль"):
        await tl.submit_code(1, password="wrong")


# ── Валидация входа / состояние ───────────────────────────────────────────────


async def test_begin_requires_api_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeClient())
    with pytest.raises(tl.TelegramLoginError, match="api_id"):
        await tl.begin(1, api_id=0, api_hash="", target_channel="@ch")


async def test_submit_without_flow_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(tl.TelegramLoginError, match="Флоу входа не начат"):
        await tl.submit_code(99, code="12345")


async def test_profile_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    c1 = FakeClient(saved_session="S1")
    c2 = FakeClient(saved_session="S2")
    clients = {1: c1, 2: c2}
    # Каждый begin берёт клиента своего профиля через замыкание последнего api_hash.
    monkeypatch.setattr(tl, "_build_client", lambda api_id, api_hash: clients[api_id])

    await tl.begin(1, api_id=1, api_hash="h", target_channel="@a")
    await tl.begin(2, api_id=2, api_hash="h", target_channel="@b")
    await tl.request_code(1, "+711")
    await tl.request_code(2, "+722")

    r1 = await tl.submit_code(1, code="1")
    assert r1.session_string == "S1"
    assert r1.target_channel == "@a"
    # Флоу профиля 2 нетронут.
    assert 2 in tl._LOGIN_SESSIONS
    r2 = await tl.submit_code(2, code="2")
    assert r2.session_string == "S2"
    assert r2.target_channel == "@b"


# ── Сериализация учётки ───────────────────────────────────────────────────────


def test_credential_blob_roundtrip() -> None:
    blob = tl.build_credential_blob(
        api_id=42, api_hash="hash", target_channel="@ch", session="SESS"
    )
    data = tl.parse_credential_blob(blob)
    assert data == {
        "api_id": 42,
        "api_hash": "hash",
        "target_channel": "@ch",
        "session": "SESS",
    }


def test_parse_bad_blob_returns_empty() -> None:
    assert tl.parse_credential_blob("not json") == {}
    assert tl.parse_credential_blob("[1,2,3]") == {}
