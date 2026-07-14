"""Тесты validate_connection: мок-валидаторы, изоляция профилей, запись в logs.

Все тесты работают без реального браузера, API и сети.
Реестр VALIDATORS подменяется через monkeypatch.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.channels.connection_validator import validate_connection
from crosspost.channels.validators import VALIDATORS, ChannelValidatorDef
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ConnectionState, CredentialKind, Log
from crosspost.db.profile_repo import ProfileRepository

# ── Фикстуры ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest.fixture
def vault() -> Fernet:
    return Fernet(Fernet.generate_key())


@pytest_asyncio.fixture
async def repo(session, vault) -> ProfileRepository:
    return ProfileRepository(session, vault=vault)


def _make_def(alive: bool, kind: CredentialKind = CredentialKind.API_TOKEN) -> ChannelValidatorDef:
    """Фиктивный валидатор, возвращающий константный результат."""

    async def _fn(credential: str | None) -> bool:
        return alive

    return ChannelValidatorDef(kind="api", credential_kind=kind, fn=_fn)


def _make_browser_def(alive: bool) -> ChannelValidatorDef:
    """Фиктивный браузерный валидатор."""

    async def _fn(credential: str | None) -> bool:
        return alive

    return ChannelValidatorDef(kind="browser", credential_kind=CredentialKind.STORAGE_STATE, fn=_fn)


# ── API-тир: telegram ─────────────────────────────────────────────────────────


async def test_api_success_writes_live(repo, monkeypatch):
    """Успешный тестовый запрос → connections.state = live."""
    monkeypatch.setitem(VALIDATORS, "telegram", _make_def(alive=True))

    p = await repo.create_profile("alice")
    state = await validate_connection(repo, p.id, "telegram")

    assert state == ConnectionState.LIVE
    conn = await repo.get_connection(p.id, "telegram")
    assert conn is not None
    assert conn.state == ConnectionState.LIVE


async def test_api_auth_failure_writes_needs_relogin(repo, monkeypatch):
    """Ошибка авторизации → connections.state = needs_relogin."""
    monkeypatch.setitem(VALIDATORS, "telegram", _make_def(alive=False))

    p = await repo.create_profile("alice")
    state = await validate_connection(repo, p.id, "telegram")

    assert state == ConnectionState.NEEDS_RELOGIN
    conn = await repo.get_connection(p.id, "telegram")
    assert conn is not None
    assert conn.state == ConnectionState.NEEDS_RELOGIN


async def test_api_passes_credential_to_validator(repo, monkeypatch, vault):
    """Валидатор получает расшифрованный токен из vault."""
    received: list[str | None] = []

    async def _capture_fn(credential: str | None) -> bool:
        received.append(credential)
        return True

    monkeypatch.setitem(
        VALIDATORS,
        "telegram",
        ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_capture_fn),
    )

    p = await repo.create_profile("alice")
    await repo.set_credential(p.id, "telegram", CredentialKind.API_TOKEN, "real-token-value")
    await validate_connection(repo, p.id, "telegram")

    assert received == ["real-token-value"]


async def test_api_no_credential_passes_none(repo, monkeypatch):
    """Нет учётки в vault → fn получает None."""
    received: list[str | None] = []

    async def _capture_fn(credential: str | None) -> bool:
        received.append(credential)
        return False

    monkeypatch.setitem(
        VALIDATORS,
        "telegram",
        ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_capture_fn),
    )

    p = await repo.create_profile("alice")
    # не сохраняем учётку
    await validate_connection(repo, p.id, "telegram")

    assert received == [None]


# ── Браузерный тир: vk_wall ───────────────────────────────────────────────────


async def test_browser_success_writes_live(repo, monkeypatch):
    """Браузер: is_logged_in=True → live."""
    monkeypatch.setitem(VALIDATORS, "vk_wall", _make_browser_def(alive=True))

    p = await repo.create_profile("alice")
    state = await validate_connection(repo, p.id, "vk_wall")

    assert state == ConnectionState.LIVE


async def test_browser_not_logged_in_writes_needs_relogin(repo, monkeypatch):
    """Браузер: is_logged_in=False → needs_relogin."""
    monkeypatch.setitem(VALIDATORS, "vk_wall", _make_browser_def(alive=False))

    p = await repo.create_profile("alice")
    state = await validate_connection(repo, p.id, "vk_wall")

    assert state == ConnectionState.NEEDS_RELOGIN


# ── Исключения и неизвестные каналы ──────────────────────────────────────────


async def test_validator_exception_writes_needs_relogin(repo, monkeypatch):
    """Исключение внутри fn → needs_relogin (не падаем наружу)."""

    async def _boom(credential: str | None) -> bool:
        raise RuntimeError("network error")

    monkeypatch.setitem(
        VALIDATORS,
        "telegram",
        ChannelValidatorDef(kind="api", credential_kind=CredentialKind.API_TOKEN, fn=_boom),
    )

    p = await repo.create_profile("alice")
    state = await validate_connection(repo, p.id, "telegram")

    assert state == ConnectionState.NEEDS_RELOGIN


async def test_unknown_channel_writes_needs_relogin(repo, monkeypatch):
    """Канала нет в реестре → needs_relogin."""
    # убедимся, что канала нет
    monkeypatch.setitem(VALIDATORS, "nonexistent", None)  # type: ignore[arg-type]
    # удаляем из реестра, если вдруг попал
    VALIDATORS.pop("nonexistent", None)

    p = await repo.create_profile("alice")
    state = await validate_connection(repo, p.id, "nonexistent")

    assert state == ConnectionState.NEEDS_RELOGIN


# ── Логирование в таблицу logs ────────────────────────────────────────────────


async def test_success_writes_info_log(repo, session, monkeypatch):
    """Успешная валидация записывает INFO в logs."""
    monkeypatch.setitem(VALIDATORS, "telegram", _make_def(alive=True))

    p = await repo.create_profile("alice")
    await validate_connection(repo, p.id, "telegram")

    rows = (
        (
            await session.execute(
                select(Log).where(Log.profile_id == p.id, Log.channel == "telegram")
            )
        )
        .scalars()
        .all()
    )
    assert any(r.level == "INFO" for r in rows)


async def test_failure_writes_warning_log(repo, session, monkeypatch):
    """Неуспешная валидация записывает WARNING в logs."""
    monkeypatch.setitem(VALIDATORS, "telegram", _make_def(alive=False))

    p = await repo.create_profile("alice")
    await validate_connection(repo, p.id, "telegram")

    rows = (
        (
            await session.execute(
                select(Log).where(Log.profile_id == p.id, Log.channel == "telegram")
            )
        )
        .scalars()
        .all()
    )
    assert any(r.level == "WARNING" for r in rows)


# ── Изоляция профилей ─────────────────────────────────────────────────────────


async def test_validate_writes_only_to_own_profile(repo, monkeypatch):
    """Результат валидации профиля A не попадает в профиль B."""
    monkeypatch.setitem(VALIDATORS, "telegram", _make_def(alive=True))

    a = await repo.create_profile("alice")
    b = await repo.create_profile("bob")

    await validate_connection(repo, a.id, "telegram")

    # у bob нет записи
    conn_b = await repo.get_connection(b.id, "telegram")
    assert conn_b is None


async def test_validate_updates_existing_connection(repo, monkeypatch):
    """Повторная валидация обновляет state, не дублирует строку."""
    p = await repo.create_profile("alice")

    # первый раз — fail
    monkeypatch.setitem(VALIDATORS, "telegram", _make_def(alive=False))
    await validate_connection(repo, p.id, "telegram")

    # второй раз — success
    monkeypatch.setitem(VALIDATORS, "telegram", _make_def(alive=True))
    await validate_connection(repo, p.id, "telegram")

    conns = await repo.get_connections(p.id)
    assert len(conns) == 1  # одна строка, не две
    assert conns[0].state == ConnectionState.LIVE
