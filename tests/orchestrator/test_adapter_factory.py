"""Тесты build_profile_adapter: per-profile цель постинга, изоляция. Итерация 2а.

Ключевой сценарий бага: один аккаунт ВК, но разные группы у клиентов —
адаптер профиля должен постить в ЕГО группу, не в чужую/глобальную из env.
Playwright не нужен: конструктор адаптера лишь хранит поля.
"""

from __future__ import annotations

import json

import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import CredentialKind
from crosspost.db.profile_repo import ProfileRepository
from crosspost.orchestrator.adapter_factory import build_profile_adapter

_STATE = json.dumps({"cookies": [{"name": "remixsid", "domain": ".vk.com"}], "origins": []})


@pytest_asyncio.fixture
async def session():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def repo(session) -> ProfileRepository:
    return ProfileRepository(session, vault=Fernet(Fernet.generate_key()))


async def test_vk_wall_adapter_uses_profile_target(repo):
    p = await repo.create_profile("A")
    await repo.set_credential(p.id, "vk_wall", CredentialKind.STORAGE_STATE, _STATE)
    await repo.set_credential(p.id, "vk_wall", CredentialKind.TARGET, "group_A")

    adapter = await build_profile_adapter(repo, p.id, "vk_wall")
    assert adapter is not None
    assert adapter._screen_name == "group_A"  # постит в ГРУППУ ЭТОГО профиля
    assert adapter._storage_state == _STATE


async def test_no_target_no_adapter(repo):
    """Есть сессия, но НЕТ per-profile цели → None (не постим в чужую группу)."""
    p = await repo.create_profile("A")
    await repo.set_credential(p.id, "vk_wall", CredentialKind.STORAGE_STATE, _STATE)
    # цель не задана
    assert await build_profile_adapter(repo, p.id, "vk_wall") is None


async def test_two_profiles_same_account_different_groups(repo):
    """Один аккаунт (одинаковая сессия), но РАЗНЫЕ группы → разные адаптеры."""
    a = await repo.create_profile("A")
    b = await repo.create_profile("B")
    for p, grp in ((a, "group_A"), (b, "group_B")):
        await repo.set_credential(p.id, "vk_wall", CredentialKind.STORAGE_STATE, _STATE)
        await repo.set_credential(p.id, "vk_wall", CredentialKind.TARGET, grp)

    ad_a = await build_profile_adapter(repo, a.id, "vk_wall")
    ad_b = await build_profile_adapter(repo, b.id, "vk_wall")
    assert ad_a._screen_name == "group_A"
    assert ad_b._screen_name == "group_B"  # B постит в СВОЮ группу, не в A


async def test_vk_channel_uses_vk_wall_session_but_own_target(repo):
    """vk_channel делит сессию vk_wall, но цель (id канала) — своя."""
    p = await repo.create_profile("A")
    await repo.set_credential(p.id, "vk_wall", CredentialKind.STORAGE_STATE, _STATE)
    await repo.set_credential(p.id, "vk_channel", CredentialKind.TARGET, "-999")

    adapter = await build_profile_adapter(repo, p.id, "vk_channel")
    assert adapter is not None
    assert adapter._channel_id == "-999"
    assert adapter._storage_state == _STATE  # сессия из vk_wall


async def test_no_session_no_adapter(repo):
    p = await repo.create_profile("A")
    await repo.set_credential(p.id, "vk_wall", CredentialKind.TARGET, "group_A")
    # сессии нет → None
    assert await build_profile_adapter(repo, p.id, "vk_wall") is None
