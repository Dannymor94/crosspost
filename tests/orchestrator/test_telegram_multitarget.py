"""Telegram: цель расцеплена с сессией (мультицели, Блок 6).

Раньше цель Telegram жила ВНУТРИ JSON-блоба сессии (target_channel) — один канал
на аккаунт. Теперь одна сессия обслуживает НЕСКОЛЬКО целей: build_profile_adapter
резолвит явную цель из выбранной ChannelTarget, а блоб остаётся только сидом
первой цели и фолбэком для legacy-вызова без target_id.

telethon мокается через sys.modules — ни сети, ни ключей.
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.channels.telegram_login import build_credential_blob
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import CredentialKind
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.target_repo import ChannelTargetRepository
from crosspost.orchestrator.adapter_factory import build_profile_adapter


@pytest.fixture
def fake_telethon(monkeypatch):
    """telethon.TelegramClient с async connect() — конструктор адаптера без сети."""
    mod = types.ModuleType("telethon")
    client = MagicMock(name="TelegramClient()")
    client.connect = AsyncMock()
    mod.TelegramClient = MagicMock(name="TelegramClient", return_value=client)
    monkeypatch.setitem(sys.modules, "telethon", mod)

    sessions_mod = types.ModuleType("telethon.sessions")
    sessions_mod.StringSession = MagicMock(name="StringSession")
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions_mod)
    return mod


@pytest_asyncio.fixture
async def session():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def repo(session) -> ProfileRepository:
    return ProfileRepository(session, vault=Fernet(Fernet.generate_key()))


def _blob(target: str = "@seed_channel") -> str:
    return build_credential_blob(
        api_id=123, api_hash="deadbeef", target_channel=target, session="SESSION_STRING"
    )


async def _connect_telegram(repo: ProfileRepository, target_channel: str = "@seed_channel") -> int:
    p = await repo.create_profile("A")
    await repo.set_credential(p.id, "telegram", CredentialKind.API_TOKEN, _blob(target_channel))
    return p.id


async def test_telegram_uses_explicit_target_over_blob(repo, fake_telethon):
    """С target_id адаптер шлёт в ВЫБРАННУЮ цель, а не в цель из блоба."""
    pid = await _connect_telegram(repo, "@seed_channel")
    trepo = ChannelTargetRepository(repo.session, vault=repo.vault, profile_id=pid)
    t1 = await trepo.add_target("telegram", "Канал А", "@channel_a")
    t2 = await trepo.add_target("telegram", "Канал Б", "@channel_b")

    ad1 = await build_profile_adapter(repo, pid, "telegram", str(t1.id))
    ad2 = await build_profile_adapter(repo, pid, "telegram", str(t2.id))
    assert ad1 is not None and ad2 is not None
    assert ad1._target == "@channel_a"
    assert ad2._target == "@channel_b"  # одна сессия, разные цели


async def test_telegram_one_session_many_targets(repo, fake_telethon):
    """Обе цели строятся из ОДНОЙ сессии (StringSession с тем же значением)."""
    pid = await _connect_telegram(repo, "@seed_channel")
    trepo = ChannelTargetRepository(repo.session, vault=repo.vault, profile_id=pid)
    t1 = await trepo.add_target("telegram", "А", "@channel_a")
    t2 = await trepo.add_target("telegram", "Б", "@channel_b")

    await build_profile_adapter(repo, pid, "telegram", str(t1.id))
    await build_profile_adapter(repo, pid, "telegram", str(t2.id))

    # StringSession инстанцировался с одной и той же строкой сессии для обеих целей.
    session_calls = [
        c.args[0] for c in sys.modules["telethon.sessions"].StringSession.call_args_list
    ]
    assert session_calls == ["SESSION_STRING", "SESSION_STRING"]


async def test_telegram_falls_back_to_blob_target_when_no_target_id(repo, fake_telethon):
    """Legacy-вызов без target_id → цель берётся из блоба (не ломаем подключения)."""
    pid = await _connect_telegram(repo, "@legacy_channel")

    adapter = await build_profile_adapter(repo, pid, "telegram")
    assert adapter is not None
    assert adapter._target == "@legacy_channel"


async def test_telegram_lazy_seeds_first_target_from_blob(repo, fake_telethon):
    """Первый list_targets синтезирует «Основную» из блоба target_channel."""
    pid = await _connect_telegram(repo, "@seed_channel")
    trepo = ChannelTargetRepository(repo.session, vault=repo.vault, profile_id=pid)

    targets = await trepo.list_targets("telegram")
    assert len(targets) == 1
    assert targets[0].name == "Основная"
    assert trepo.decrypt_value(targets[0]) == "@seed_channel"


async def test_telegram_incomplete_blob_no_adapter(repo, fake_telethon):
    """Неполный блоб (нет сессии) → None, даже если цель выбрана."""
    p = await repo.create_profile("A")
    bad = json.dumps({"api_id": 1, "api_hash": "h", "target_channel": "@c"})  # session отсутствует
    await repo.set_credential(p.id, "telegram", CredentialKind.API_TOKEN, bad)

    assert await build_profile_adapter(repo, p.id, "telegram") is None
