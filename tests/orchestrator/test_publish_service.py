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

    async def factory(channel, target_id=""):
        return mapping.get(channel)

    return factory


def _repo(session, profile_id=1) -> PublicationRepository:
    return PublicationRepository(session, profile_id=profile_id)


# ── Мульти-канал / частичный успех ───────────────────────────────────────────


async def test_publish_multichannel_all_done(session):
    adapters = {
        "telegram": _MockAdapter(
            "telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1")
        ),
        "vk_wall": _MockAdapter(
            "vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vk1")
        ),
    }
    svc = PublishService(_repo(session), _factory(adapters))

    outcomes = await svc.publish(_post(), ["telegram", "vk_wall"], publication_id="p1")

    assert {o.channel: o.status for o in outcomes} == {"telegram": "done", "vk_wall": "done"}
    assert {o.external_id for o in outcomes} == {"tg1", "vk1"}


async def test_channel_url_forwarded_but_not_persisted(session):
    """result.url прокидывается в ChannelOutcome, но в БД НЕ пишется (временная ссылка)."""
    adapters = {
        "vk_channel": _MockAdapter(
            "vk_channel",
            result=ChannelResult(
                "vk_channel",
                ResultStatus.DONE,
                external_id="channel-item11",
                url="https://vk.ru/im/channels/-240033402?entrypoint=channel",
            ),
        ),
    }
    svc = PublishService(_repo(session), _factory(adapters))

    outcomes = await svc.publish(_post(), ["vk_channel"], publication_id="p1")

    assert outcomes[0].url == "https://vk.ru/im/channels/-240033402?entrypoint=channel"
    # В БД сохраняются только status/external_id/error — url там нет.
    row = (await _repo(session).list_statuses("p1"))[0]
    assert not hasattr(row, "url") or getattr(row, "url", None) is None


async def test_partial_success_one_fails(session):
    adapters = {
        "telegram": _MockAdapter(
            "telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1")
        ),
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
        "yandex": _MockAdapter(
            "yandex", result=ChannelResult("yandex", ResultStatus.SUBMITTED, "y1")
        ),
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

    async def factory(channel, target_id=""):
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


# ── Связка статья→канал (article_text) ────────────────────────────────────────


class _CapturingAdapter:
    """Мок: запоминает text каждого publish (для проверки подстановки url)."""

    def __init__(self, channel, *, result=None, raises=None):
        self.channel = channel
        self._result = result
        self._raises = raises
        self.texts: list[str] = []
        self.calls = 0

    async def publish(self, content, *, publication_id):
        self.calls += 1
        self.texts.append(content.text)
        if self._raises is not None:
            raise self._raises
        return self._result or ChannelResult(self.channel, ResultStatus.DONE, external_id="x1")


async def test_publish_with_article_creates_telegraph_first_and_substitutes_url(session):
    url = "https://telegra.ph/Long-08-12"
    tgraph = _CapturingAdapter(
        "telegraph", result=ChannelResult("telegraph", ResultStatus.DONE, external_id=url)
    )
    tg = _CapturingAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    svc = PublishService(
        session and _repo(session), _factory({"telegraph": tgraph, "telegram": tg})
    )

    outcomes = await svc.publish(
        CanonicalContent(type=ContentType.POST, text="Короткий", media_paths=[Path("a.jpg")]),
        ["telegram"],
        publication_id="p1",
        article_text="Тело лонгрида",
        article_title="Заголовок",
    )
    # Статья создана раньше, телом ушёл article_text.
    assert tgraph.calls == 1
    assert tgraph.texts == ["Тело лонгрида"]
    # В текст TG подставлен url статьи.
    assert url in tg.texts[0]
    assert "Короткий" in tg.texts[0]
    # Итоги включают и telegraph, и telegram.
    assert {o.channel for o in outcomes} == {"telegram", "telegraph"}


async def test_publish_article_failed_fail_closed_channel_not_sent(session):
    from crosspost.content.article_link import NO_ARTICLE_REASON

    tgraph = _CapturingAdapter(
        "telegraph", result=ChannelResult("telegraph", ResultStatus.FAILED, error="boom")
    )
    tg = _CapturingAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    svc = PublishService(_repo(session), _factory({"telegraph": tgraph, "telegram": tg}))

    outcomes = await svc.publish(
        CanonicalContent(type=ContentType.POST, text="Короткий", media_paths=[Path("a.jpg")]),
        ["telegram"],
        publication_id="p1",
        article_text="Тело",
    )
    # TG не публиковался (fail-closed).
    assert tg.calls == 0
    by = {o.channel: o for o in outcomes}
    assert by["telegram"].status == "failed"
    assert by["telegram"].error == NO_ARTICLE_REASON
    # Персистентно: TG помечен failed с причиной.
    statuses = {
        p.channel: (str(p.status), p.error) for p in await _repo(session).list_statuses("p1")
    }
    assert statuses["telegram"][0] == "failed"
    assert statuses["telegram"][1] == NO_ARTICLE_REASON


async def test_publish_article_idempotent_no_second_page(session):
    url = "https://telegra.ph/Long-08-12"
    tgraph = _CapturingAdapter(
        "telegraph", result=ChannelResult("telegraph", ResultStatus.DONE, external_id=url)
    )
    tg = _CapturingAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    svc = PublishService(_repo(session), _factory({"telegraph": tgraph, "telegram": tg}))

    args = {
        "channels": ["telegram"],
        "publication_id": "p1",
        "article_text": "Тело",
        "article_title": "T",
    }
    content = CanonicalContent(type=ContentType.POST, text="Короткий", media_paths=[Path("a.jpg")])
    await svc.publish(content, **args)
    await svc.publish(content, **args)  # повтор — вторая страница не создаётся
    assert tgraph.calls == 1
    assert tg.calls == 1


# ── Мультицели (несколько целей одного канала) ────────────────────────────────


def _target_factory(mapping: dict):
    """Фабрика по ключу (channel, target_id) → adapter. Пишет вызовы в calls_log."""
    calls_log: list[tuple[str, str]] = []

    async def factory(channel, target_id=""):
        calls_log.append((channel, target_id))
        return mapping.get((channel, target_id))

    return factory, calls_log


async def test_fanout_to_two_targets_of_same_channel(session):
    """Один канал, две цели → две независимые единицы с общим publication_id."""
    from crosspost.orchestrator.publish_service import Selection

    a = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vkA"))
    b = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vkB"))
    factory, log = _target_factory({("vk_wall", "10"): a, ("vk_wall", "20"): b})
    svc = PublishService(_repo(session), factory)

    outcomes = await svc.publish(
        _post(),
        [Selection("vk_wall", "10"), Selection("vk_wall", "20")],
        publication_id="p1",
    )

    # Обе цели опубликованы, у каждой своя квитанция и своё измерение target.
    assert log == [("vk_wall", "10"), ("vk_wall", "20")]
    by_target = {o.target: (o.status, o.external_id) for o in outcomes}
    assert by_target == {"10": ("done", "vkA"), "20": ("done", "vkB")}
    # В БД — две строки под одним publication_id, не коллизят.
    rows = await _repo(session).list_statuses("p1")
    assert {(r.channel, r.target): str(r.status) for r in rows} == {
        ("vk_wall", "10"): "done",
        ("vk_wall", "20"): "done",
    }


async def test_partial_success_across_targets(session):
    """Одна цель канала упала, другая прошла — частичный успех по целям."""
    from crosspost.orchestrator.publish_service import Selection

    ok = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vkA"))
    bad = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.FAILED, error="no"))
    factory, _ = _target_factory({("vk_wall", "10"): ok, ("vk_wall", "20"): bad})
    svc = PublishService(_repo(session), factory)

    outcomes = await svc.publish(
        _post(), [Selection("vk_wall", "10"), Selection("vk_wall", "20")], publication_id="p1"
    )
    assert {o.target: o.status for o in outcomes} == {"10": "done", "20": "failed"}


async def test_retry_one_target_leaves_other_untouched(session):
    """Ретрай упавшей цели не трогает успешную (без повторной отправки в неё)."""
    from crosspost.orchestrator.publish_service import Selection

    ok = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vkA"))
    bad = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.FAILED, error="no"))
    factory, _ = _target_factory({("vk_wall", "10"): ok, ("vk_wall", "20"): bad})
    svc = PublishService(_repo(session), factory)
    await svc.publish(
        _post(), [Selection("vk_wall", "10"), Selection("vk_wall", "20")], publication_id="p1"
    )
    assert ok.calls == 1 and bad.calls == 1

    # Чиним цель 20 и ретраим ТОЛЬКО её.
    fixed = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vkB"))
    factory2, _ = _target_factory({("vk_wall", "10"): ok, ("vk_wall", "20"): fixed})
    svc2 = PublishService(_repo(session), factory2)
    out = await svc2.retry_channel(_post(), Selection("vk_wall", "20"), publication_id="p1")

    assert out.status == "done" and out.target == "20"
    assert fixed.calls == 1
    assert ok.calls == 1  # успешная цель не пересылалась


async def test_idempotent_per_target_second_publish_skips(session):
    """Повторная публикация в уже-done цель — SKIP (адаптер не вызывается снова)."""
    from crosspost.orchestrator.publish_service import Selection

    a = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "vkA"))
    factory, _ = _target_factory({("vk_wall", "10"): a})
    svc = PublishService(_repo(session), factory)

    await svc.publish(_post(), [Selection("vk_wall", "10")], publication_id="p1")
    await svc.publish(_post(), [Selection("vk_wall", "10")], publication_id="p1")  # повтор
    assert a.calls == 1  # вторая публикация в ту же цель не пошла в адаптер


async def test_string_channel_still_works_as_targetless_selection(session):
    """Старый вызов publish со строками-каналами эквивалентен Selection(ch, '')."""
    a = _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    factory, log = _target_factory({("telegram", ""): a})
    svc = PublishService(_repo(session), factory)

    outcomes = await svc.publish(_post(), ["telegram"], publication_id="p1")
    assert log == [("telegram", "")]
    assert outcomes[0].target == ""
    assert outcomes[0].status == "done"
