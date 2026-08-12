"""Тесты LogRepository — кросс-профильная лента и сводка для админ-консоли (Трек C).

Проверяем: фильтр по уровням/клиенту/каналу/периоду, курсорную пагинацию по id
(before/since), джойн имени клиента, сводку (число клиентов с проблемами + разбивка
по каналам). Изоляция здесь не проверяется — она на роут-слое (require_admin).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.log_repo import LogRepository
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.vault import get_vault

pytestmark = pytest.mark.asyncio

PROBLEMS = {"WARNING", "ERROR"}


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
async def seeded(factory):
    """Два профиля (A, B) с логами разных уровней/каналов. Возвращает id + session-factory."""
    async with factory() as s:
        prepo = ProfileRepository(s, vault=get_vault())
        a = await prepo.create_profile("client-A")
        b = await prepo.create_profile("client-B")
        log = LogRepository(s)
        # A: INFO telegram, ERROR telegram, WARNING vk_wall
        await log.add(a.id, channel="telegram", level="INFO", message="ok")
        await log.add(a.id, channel="telegram", level="ERROR", message="tg failed")
        await log.add(a.id, channel="vk_wall", level="WARNING", message="vk slow")
        # B: ERROR telegram, ERROR yandex
        await log.add(b.id, channel="telegram", level="ERROR", message="tg B failed")
        await log.add(b.id, channel="yandex", level="ERROR", message="ya failed")
    return {"a": a.id, "b": b.id, "factory": factory}


async def test_only_problems_excludes_info(seeded):
    async with seeded["factory"]() as s:
        rows = await LogRepository(s).list_across_profiles(levels=PROBLEMS)
    assert all(lg.level in PROBLEMS for lg, _ in rows)
    assert len(rows) == 4  # INFO отфильтрован


async def test_show_all_includes_info(seeded):
    async with seeded["factory"]() as s:
        rows = await LogRepository(s).list_across_profiles(levels=None)
    assert len(rows) == 5
    assert any(lg.level == "INFO" for lg, _ in rows)


async def test_joins_client_name(seeded):
    async with seeded["factory"]() as s:
        rows = await LogRepository(s).list_across_profiles(levels=PROBLEMS)
    names = {name for _, name in rows}
    assert names == {"client-A", "client-B"}


async def test_filter_by_profile(seeded):
    async with seeded["factory"]() as s:
        rows = await LogRepository(s).list_across_profiles(levels=PROBLEMS, profile_id=seeded["b"])
    assert {lg.profile_id for lg, _ in rows} == {seeded["b"]}
    assert len(rows) == 2


async def test_filter_by_channel(seeded):
    async with seeded["factory"]() as s:
        rows = await LogRepository(s).list_across_profiles(levels=PROBLEMS, channel="telegram")
    assert all(lg.channel == "telegram" for lg, _ in rows)
    assert len(rows) == 2  # A ERROR + B ERROR


async def test_newest_first_order(seeded):
    async with seeded["factory"]() as s:
        rows = await LogRepository(s).list_across_profiles(levels=None)
    ids = [lg.id for lg, _ in rows]
    assert ids == sorted(ids, reverse=True)


async def test_pagination_before_and_since(seeded):
    async with seeded["factory"]() as s:
        repo = LogRepository(s)
        page1 = await repo.list_across_profiles(levels=None, limit=2)
        assert len(page1) == 2
        oldest = page1[-1][0].id
        page2 = await repo.list_across_profiles(levels=None, limit=2, before=oldest)
        assert all(lg.id < oldest for lg, _ in page2)
        # since: новее самого свежего — пусто
        newest = page1[0][0].id
        delta = await repo.list_across_profiles(levels=None, since=newest)
        assert delta == []


async def test_from_time_period_filter(seeded):
    """from_time в будущем → пусто; в прошлом → все проблемы."""
    async with seeded["factory"]() as s:
        repo = LogRepository(s)
        future = datetime.now() + timedelta(days=1)
        assert await repo.list_across_profiles(levels=PROBLEMS, from_time=future) == []
        past = datetime.now() - timedelta(days=1)
        rows = await repo.list_across_profiles(levels=PROBLEMS, from_time=past)
        assert len(rows) == 4


async def test_problem_summary(seeded):
    async with seeded["factory"]() as s:
        clients, channels = await LogRepository(s).problem_summary(levels=PROBLEMS)
    assert clients == 2  # оба клиента с проблемами
    chan_map = dict(channels)
    assert chan_map["telegram"] == 2
    assert chan_map["yandex"] == 1
    assert chan_map["vk_wall"] == 1
    # порядок — по убыванию частоты: telegram первым
    assert channels[0][0] == "telegram"


async def test_get_by_id(seeded):
    async with seeded["factory"]() as s:
        repo = LogRepository(s)
        rows = await repo.list_across_profiles(levels=PROBLEMS)
        one = rows[0][0]
        fetched = await repo.get(one.id)
        assert fetched is not None
        assert fetched.id == one.id
        assert await repo.get(999999) is None
