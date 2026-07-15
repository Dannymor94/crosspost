"""Тесты PublishService на моках адаптеров. Итерация 2а.

Проверяем: мульти-канал, частичный успех, ретрай ТОЛЬКО упавшего (без дублей в
успешные), идемпотентность (done → skip), отсечение по capability, needs_relogin
при отсутствии адаптера, submitted (Яндекс), изоляция статусов по profile_id.
"""

from __future__ import annotations

from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.adapters.base import ChannelResult, ResultStatus
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.publication_repo import PublicationRepository
from crosspost.orchestrator.publish_service import PublishService


@pytest_asyncio.fixture
async def session():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


def _post() -> CanonicalContent:
    return CanonicalContent(type=ContentType.POST, text="привет", media_paths=[Path("a.jpg")])


class _MockAdapter:
    """Мок адаптера: программируемый результат / исключение. Считает вызовы publish."""

    def __init__(self, channel: str, *, result=None, raises=None) -> None:
        self.channel = channel
        self._result = result
        self._raises = raises
        self.calls = 0

    async def publish(self, content, *, publication_id):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result or ChannelResult(self.channel, ResultStatus.DONE, external_id="x1")


def _factory(mapping: dict):
    """adapter_factory из словаря channel→adapter (или None)."""

    async def factory(channel):
        return mapping.get(channel)

    return factory


def _repo(session, profile_id=1) -> PublicationRepository:
    return PublicationRepository(session, profile_id=profile_id)


# ── Мульти-канал / частичный успех ───────────────────────────────────────────


async def test_publish_multichannel_all_done(session):
    adapters = {
        "telegram": _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1")),
        "vk_wall": _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vk1")),
    }
    svc = PublishService(_repo(session), _factory(adapters))

    outcomes = await svc.publish(_post(), ["telegram", "vk_wall"], publication_id="p1")

    assert {o.channel: o.status for o in outcomes} == {"telegram": "done", "vk_wall": "done"}
    assert {o.external_id for o in outcomes} == {"tg1", "vk1"}


async def test_partial_success_one_fails(session):
    adapters = {
        "telegram": _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1")),
        "vk_wall": _MockAdapter("vk_wall", raises=RuntimeError("VK упал")),
    }
    svc = PublishService(_repo(session), _factory(adapters))

    outcomes = await svc.publish(_post(), ["telegram", "vk_wall"], publication_id="p1")
    by = {o.channel: o for o in outcomes}

    assert by["telegram"].status == "done"
    assert by["vk_wall"].status == "failed"
    assert "VK упал" in by["vk_wall"].error
    # Персистентно: статусы в БД поканальные.
    statuses = {p.channel: str(p.status) for p in await _repo(session).list_statuses("p1")}
    assert statuses == {"telegram": "done", "vk_wall": "failed"}


async def test_yandex_submitted_not_done(session):
    adapters = {
        "yandex": _MockAdapter("yandex", result=ChannelResult("yandex", ResultStatus.SUBMITTED, "y1")),
    }
    svc = PublishService(_repo(session), _factory(adapters))
    outcomes = await svc.publish(_post(), ["yandex"], publication_id="p1")
    assert outcomes[0].status == "submitted"  # модерация ≠ опубликовано


# ── Ретрай одного канала ──────────────────────────────────────────────────────


async def test_retry_only_failed_channel_no_dupes(session):
    tg = _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    vk = _MockAdapter("vk_wall", raises=RuntimeError("VK упал"))
    svc = PublishService(_repo(session), _factory({"telegram": tg, "vk_wall": vk}))

    await svc.publish(_post(), ["telegram", "vk_wall"], publication_id="p1")
    assert tg.calls == 1 and vk.calls == 1

    # Чиним VK и ретраим ТОЛЬКО его.
    vk_ok = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vk9"))
    svc2 = PublishService(_repo(session), _factory({"telegram": tg, "vk_wall": vk_ok}))
    out = await svc2.retry_channel(_post(), "vk_wall", publication_id="p1")

    assert out.status == "done" and out.external_id == "vk9"
    # Telegram НЕ переотправлен (без дублей в успешные).
    assert tg.calls == 1
    statuses = {p.channel: str(p.status) for p in await _repo(session).list_statuses("p1")}
    assert statuses == {"telegram": "done", "vk_wall": "done"}


async def test_retry_done_channel_is_noop(session):
    tg = _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    svc = PublishService(_repo(session), _factory({"telegram": tg}))
    await svc.publish(_post(), ["telegram"], publication_id="p1")

    out = await svc.retry_channel(_post(), "telegram", publication_id="p1")
    assert out.status == "done"
    assert tg.calls == 1  # повторно не публиковали


# ── Идемпотентность / capability / нет адаптера ──────────────────────────────


async def test_idempotent_skip_when_already_done(session):
    tg = _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    svc = PublishService(_repo(session), _factory({"telegram": tg}))

    await svc.publish(_post(), ["telegram"], publication_id="p1")
    await svc.publish(_post(), ["telegram"], publication_id="p1")  # повтор
    assert tg.calls == 1  # второй publish не дошёл до адаптера


async def test_capability_blocks_unsupported_type(session):
    # yandex поддерживает только post; для reel — сразу failed, адаптер не строим.
    called = {"built": False}

    async def factory(channel):
        called["built"] = True
        return _MockAdapter(channel)

    svc = PublishService(_repo(session), factory)
    content = CanonicalContent(type=ContentType.REEL, text="", media_paths=[Path("v.mp4")])
    outcomes = await svc.publish(content, ["yandex"], publication_id="p1")

    assert outcomes[0].status == "failed"
    assert "не поддерживает" in outcomes[0].error
    assert called["built"] is False  # адаптер даже не строился


async def test_missing_adapter_marks_needs_relogin(session):
    svc = PublishService(_repo(session), _factory({}))  # адаптера нет → None
    outcomes = await svc.publish(_post(), ["telegram"], publication_id="p1")
    assert outcomes[0].status == "needs_relogin"


# ── Изоляция по profile_id ───────────────────────────────────────────────────


async def test_status_isolation_between_profiles(session):
    tg = _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    svc_a = PublishService(_repo(session, profile_id=1), _factory({"telegram": tg}))
    await svc_a.publish(_post(), ["telegram"], publication_id="shared")

    # Профиль 2 с тем же publication_id — своих статусов нет.
    repo_b = _repo(session, profile_id=2)
    assert await repo_b.list_statuses("shared") == []
    assert await repo_b.is_done("shared", "telegram") is False
