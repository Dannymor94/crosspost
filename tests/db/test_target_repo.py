"""Тесты ChannelTargetRepository — мультицели канала (Milestone «мультицели»).

Проверяем: CRUD целей, изоляцию по profile_id, шифрование value в БД,
ленивый сид первой цели из legacy-одиночной цели (браузерный CredentialKind.TARGET
и telegram-блоб target_channel), отсутствие дублирующего сида при повторном чтении.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.channels.telegram_login import build_credential_blob
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import ChannelTarget, CredentialKind
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.target_repo import ChannelTargetRepository
from crosspost.db.vault import get_vault

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


@pytest_asyncio.fixture
async def profiles(factory):
    """Два профиля A, B для проверки изоляции."""
    async with factory() as s:
        prepo = ProfileRepository(s, vault=get_vault())
        a = await prepo.create_profile("client-A")
        b = await prepo.create_profile("client-B")
    return {"a": a.id, "b": b.id, "factory": factory}


def _repo(s: AsyncSession, profile_id: int) -> ChannelTargetRepository:
    return ChannelTargetRepository(s, vault=get_vault(), profile_id=profile_id)


# ── CRUD ────────────────────────────────────────────────────────────────────


async def test_add_and_list(profiles):
    async with profiles["factory"]() as s:
        repo = _repo(s, profiles["a"])
        await repo.add_target("vk_wall", "Клуб А", "clubA")
        await repo.add_target("vk_wall", "Клуб Б", "clubB")
        rows = await repo.list_targets("vk_wall")
    assert [t.name for t in rows] == ["Клуб А", "Клуб Б"]
    assert [repo.decrypt_value(t) for t in rows] == ["clubA", "clubB"]


async def test_rename_and_set_value(profiles):
    async with profiles["factory"]() as s:
        repo = _repo(s, profiles["a"])
        t = await repo.add_target("vk_wall", "Старое", "old")
        assert await repo.rename_target(t.id, "Новое") is True
        assert await repo.set_value(t.id, "new") is True
        got = await repo.get_target(t.id)
    assert got is not None
    assert got.name == "Новое"
    assert repo.decrypt_value(got) == "new"


async def test_delete(profiles):
    async with profiles["factory"]() as s:
        repo = _repo(s, profiles["a"])
        t = await repo.add_target("vk_wall", "Ц", "v")
        assert await repo.delete_target(t.id) is True
        assert await repo.delete_target(t.id) is False  # идемпотентно
        assert await repo.list_targets("vk_wall") == []


async def test_value_encrypted_at_rest(profiles):
    """В столбце value_blob — шифртекст, не открытый текст."""
    async with profiles["factory"]() as s:
        repo = _repo(s, profiles["a"])
        await repo.add_target("vk_wall", "Ц", "secret-community")
        raw = (await s.execute(select(ChannelTarget.value_blob))).scalar_one()
    assert "secret-community" not in raw


# ── Изоляция ─────────────────────────────────────────────────────────────────


async def test_isolation_between_profiles(profiles):
    async with profiles["factory"]() as s:
        ra = _repo(s, profiles["a"])
        rb = _repo(s, profiles["b"])
        ta = await ra.add_target("vk_wall", "A-цель", "a")
        await rb.add_target("vk_wall", "B-цель", "b")
        # B не видит цель A ни в списке, ни по id
        assert [t.name for t in await rb.list_targets("vk_wall")] == ["B-цель"]
        assert await rb.get_target(ta.id) is None
        assert await rb.rename_target(ta.id, "hack") is False
        assert await rb.delete_target(ta.id) is False


# ── Ленивый сид из legacy ────────────────────────────────────────────────────


async def test_seed_from_legacy_browser_target(profiles):
    """Старая одиночная цель (CredentialKind.TARGET) → первая цель «Основная»."""
    async with profiles["factory"]() as s:
        prepo = ProfileRepository(s, vault=get_vault())
        await prepo.set_credential(profiles["a"], "vk_wall", CredentialKind.TARGET, "legacyclub")
        rows = await _repo(s, profiles["a"]).list_targets("vk_wall")
    assert len(rows) == 1
    assert rows[0].name == "Основная"
    assert (
        ChannelTargetRepository(s, vault=get_vault(), profile_id=profiles["a"]).decrypt_value(
            rows[0]
        )
        == "legacyclub"
    )


async def test_seed_from_telegram_blob(profiles):
    """Старый target_channel из telegram-блоба → первая цель."""
    blob = build_credential_blob(
        api_id=1, api_hash="h", target_channel="@mychannel", session="sess"
    )
    async with profiles["factory"]() as s:
        prepo = ProfileRepository(s, vault=get_vault())
        await prepo.set_credential(profiles["a"], "telegram", CredentialKind.API_TOKEN, blob)
        repo = _repo(s, profiles["a"])
        rows = await repo.list_targets("telegram")
    assert len(rows) == 1
    assert repo.decrypt_value(rows[0]) == "@mychannel"


async def test_seed_runs_once(profiles):
    """Повторный list_targets не дублирует засиженную цель."""
    async with profiles["factory"]() as s:
        prepo = ProfileRepository(s, vault=get_vault())
        await prepo.set_credential(profiles["a"], "vk_wall", CredentialKind.TARGET, "club")
        repo = _repo(s, profiles["a"])
        await repo.list_targets("vk_wall")
        rows2 = await repo.list_targets("vk_wall")
    assert len(rows2) == 1


async def test_no_seed_without_legacy(profiles):
    """Нет legacy-цели → список пуст (нечего сидировать)."""
    async with profiles["factory"]() as s:
        rows = await _repo(s, profiles["a"]).list_targets("yandex")
    assert rows == []
