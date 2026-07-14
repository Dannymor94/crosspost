"""Тесты SQLiteIdempotencyStore и базовых операций с таблицей publications.

Без реального файла БД — используем in-memory SQLite через aiosqlite.
Покрываем:
  - is_done: False до mark_done, True после
  - mark_done дважды — идемпотентно, не бросает
  - mark_done с external_id сохраняет квитанцию
  - изоляция profile_id: чужая запись не видна через is_done
  - get_external_id: возвращает квитанцию
  - попытки (attempt_count) увеличиваются через increment_attempt
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.sqlite_store import SQLiteIdempotencyStore


@pytest_asyncio.fixture
async def session():
    """In-memory SQLite: схема создаётся при старте, исчезает после теста."""
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def store(session):
    return SQLiteIdempotencyStore(session, profile_id=1)


@pytest_asyncio.fixture
async def other_store(session):
    """Тот же сеанс, но другой profile_id — для проверки изоляции."""
    return SQLiteIdempotencyStore(session, profile_id=2)


# ── тесты ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_done_false_initially(store):
    assert not await store.is_done("pub-1", "telegram")


@pytest.mark.asyncio
async def test_mark_done_sets_is_done(store):
    await store.mark_done("pub-1", "telegram", external_id="msg:42")
    assert await store.is_done("pub-1", "telegram")


@pytest.mark.asyncio
async def test_mark_done_idempotent(store):
    await store.mark_done("pub-1", "telegram", external_id="msg:42")
    await store.mark_done("pub-1", "telegram", external_id="msg:42")  # второй раз — ок
    assert await store.is_done("pub-1", "telegram")


@pytest.mark.asyncio
async def test_mark_done_stores_external_id(store):
    await store.mark_done("pub-2", "yandex", external_id="yandex_post:99")
    ext = await store.get_external_id("pub-2", "yandex")
    assert ext == "yandex_post:99"


@pytest.mark.asyncio
async def test_mark_done_without_external_id(store):
    await store.mark_done("pub-3", "vk_wall", external_id=None)
    assert await store.is_done("pub-3", "vk_wall")
    assert await store.get_external_id("pub-3", "vk_wall") is None


@pytest.mark.asyncio
async def test_profile_isolation(store, other_store):
    """profile_id=1 не видит записи profile_id=2."""
    await other_store.mark_done("pub-x", "telegram", external_id="other")
    assert not await store.is_done("pub-x", "telegram")


@pytest.mark.asyncio
async def test_increment_attempt(store):
    await store.mark_done("pub-4", "telegram", external_id=None)
    count = await store.increment_attempt("pub-4", "telegram")
    assert count == 1
    count = await store.increment_attempt("pub-4", "telegram")
    assert count == 2
