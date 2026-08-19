"""Тесты ProfileSettingsRepository — несекретные настройки профиля (импорт).

Проверяем: get/set JSON-значения, upsert (перезапись), изоляцию по profile_id,
типизированные хелперы для маппинга колонок и папки-источника картинок.
Это НЕ секреты — открытый JSON, без vault.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.settings_repo import ProfileSettingsRepository
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
    async with factory() as s:
        prepo = ProfileRepository(s, vault=get_vault())
        a = await prepo.create_profile("client-A")
        b = await prepo.create_profile("client-B")
    return {"a": a.id, "b": b.id, "factory": factory}


def _repo(s: AsyncSession, profile_id: int) -> ProfileSettingsRepository:
    return ProfileSettingsRepository(s, profile_id=profile_id)


async def test_get_missing_returns_none(profiles):
    async with profiles["factory"]() as s:
        assert await _repo(s, profiles["a"]).get("import_column_mapping") is None


async def test_set_and_get_roundtrips(profiles):
    async with profiles["factory"]() as s:
        repo = _repo(s, profiles["a"])
        await repo.set("import_column_mapping", {"file": "Картинка", "text": "Пост"})
        got = await repo.get("import_column_mapping")
    assert got == {"file": "Картинка", "text": "Пост"}


async def test_set_upserts(profiles):
    async with profiles["factory"]() as s:
        repo = _repo(s, profiles["a"])
        await repo.set("import_media_folder", {"path": "/a"})
        await repo.set("import_media_folder", {"path": "/b"})
        got = await repo.get("import_media_folder")
    assert got == {"path": "/b"}


async def test_isolation_between_profiles(profiles):
    async with profiles["factory"]() as s:
        await _repo(s, profiles["a"]).set("import_media_folder", {"path": "/a"})
    async with profiles["factory"]() as s:
        assert await _repo(s, profiles["b"]).get("import_media_folder") is None


async def test_column_mapping_helpers(profiles):
    async with profiles["factory"]() as s:
        repo = _repo(s, profiles["a"])
        assert await repo.get_column_mapping() == {}
        await repo.set_column_mapping({"file": "Фото", "text": "Описание"})
        assert await repo.get_column_mapping() == {"file": "Фото", "text": "Описание"}


async def test_media_folder_helpers(profiles):
    async with profiles["factory"]() as s:
        repo = _repo(s, profiles["a"])
        assert await repo.get_media_folder() == ""
        await repo.set_media_folder("/srv/pics")
        assert await repo.get_media_folder() == "/srv/pics"
