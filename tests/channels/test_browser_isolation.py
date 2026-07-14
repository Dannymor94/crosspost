"""Тест изоляции браузерных сессий per-profile (критичная дыра).

Сценарий бага: профиль A подключил vk_wall (в credentials лежит его storageState),
профиль B нажимает «Войти»/«Проверить» — и НЕ должен подхватить сессию A.

Проверяем сквозь реальный validate_connection + реальный browser-валидатор из
реестра, подменив ТОЛЬКО playwright-обёртку base_browser (open_page/is_logged_in):
  - в open_page приходит ИМЕННО per-profile storage_state из credentials;
  - у профиля без сессии storage_state = None → NEEDS_RELOGIN (не подхват чужой);
  - credentials A и B физически различны.
"""

from __future__ import annotations

import json

import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.channels.connection_validator import validate_connection
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ConnectionState, CredentialKind
from crosspost.db.profile_repo import ProfileRepository

_A_STATE = {"cookies": [{"name": "remixsid", "value": "AAA"}], "origins": []}


@pytest_asyncio.fixture
async def session():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def repo(session) -> ProfileRepository:
    return ProfileRepository(session, vault=Fernet(Fernet.generate_key()))


class _FakePage:
    """Страница, «залогиненность» которой определяется наличием storage_state."""

    def __init__(self, has_session: bool) -> None:
        self.url = "https://vk.com/feed" if has_session else "https://vk.com/login"

    async def goto(self, *a, **k):
        return None


def _patch_browser(monkeypatch, seen: dict):
    """Подменить open_page/is_logged_in в base_browser.

    open_page записывает полученный storage_state и «логинит» страницу только
    если storage_state не пуст — точная имитация Playwright new_context.
    """
    from contextlib import asynccontextmanager

    from crosspost.adapters.browser import base_browser as bb

    @asynccontextmanager
    async def fake_open_page(
        channel, *, headless=False, session_channel=None, storage_state=bb._FROM_FILE
    ):
        seen["storage_state"] = storage_state
        seen["channel"] = channel
        has = bool(bb._coerce_storage_state(storage_state))
        yield _FakePage(has_session=has)

    async def fake_is_logged_in(
        page, *, reject_url_fragments=(), require_selector=None, timeout=5000
    ):
        return not any(f in page.url for f in reject_url_fragments)

    monkeypatch.setattr(bb, "open_page", fake_open_page)
    monkeypatch.setattr(bb, "is_logged_in", fake_is_logged_in)


async def test_profile_B_does_not_inherit_profile_A_session(repo, monkeypatch):
    seen: dict = {}
    _patch_browser(monkeypatch, seen)

    a = await repo.create_profile("A")
    b = await repo.create_profile("B")

    # Профиль A подключил vk_wall — его storageState в credentials.
    await repo.set_credential(a.id, "vk_wall", CredentialKind.STORAGE_STATE, json.dumps(_A_STATE))

    # A валидируется → LIVE, и open_page получил ИМЕННО сессию A.
    state_a = await validate_connection(repo, a.id, "vk_wall")
    assert state_a == ConnectionState.LIVE
    assert bool(seen["storage_state"]) is True

    # B нажимает «Проверить»/«Войти» — своей сессии нет.
    state_b = await validate_connection(repo, b.id, "vk_wall")
    assert state_b == ConnectionState.NEEDS_RELOGIN
    # Ключевое: в open_page для B пришёл ПУСТОЙ storage_state, НЕ сессия A.
    assert seen["storage_state"] in (None, "", {})

    # Credentials A и B физически различны: у B их нет.
    cred_a = await repo.get_credential(a.id, "vk_wall", CredentialKind.STORAGE_STATE)
    cred_b = await repo.get_credential(b.id, "vk_wall", CredentialKind.STORAGE_STATE)
    assert cred_a is not None
    assert cred_b is None
    assert cred_a != cred_b


async def test_vk_channel_reads_vk_wall_session(repo, monkeypatch):
    """vk_channel делит аккаунт с vk_wall: сессию берёт из credentials vk_wall профиля."""
    seen: dict = {}
    _patch_browser(monkeypatch, seen)

    a = await repo.create_profile("A")
    await repo.set_credential(a.id, "vk_wall", CredentialKind.STORAGE_STATE, json.dumps(_A_STATE))

    # Валидируем vk_channel — сессии под "vk_channel" нет, но есть под "vk_wall".
    state = await validate_connection(repo, a.id, "vk_channel")
    assert state == ConnectionState.LIVE
    assert bool(seen["storage_state"]) is True
