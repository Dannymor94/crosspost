"""Тесты исполнителя расписания. Трек A, часть 3 (+ диагностика Блок 4).

Адаптеры мокаются через патч build_profile_adapter (как в test_publish_api).
Проверяем: публикация созревшего поста, игнор будущего, пейсинг, ретрай с
потолком, идемпотентность между «перезапусками», чистка медиа, изоляция,
запись в logs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.adapters.base import ChannelResult, ResultStatus
from crosspost.db.engine import _migrate_scheduled_status, create_engine_and_tables
from crosspost.db.log_repo import LogRepository
from crosspost.db.models import (
    ConnectionState,
    CredentialKind,
    PublicationStatus,
    ScheduledPostStatus,
)
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.scheduler_repo import SchedulerRepository
from crosspost.db.vault import get_vault
from crosspost.scheduler.executor import ScheduledExecutor, scheduled_publication_id

_PATCH = "crosspost.scheduler.executor.build_profile_adapter"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


# ── Хелперы ───────────────────────────────────────────────────────────────────


class _MockAdapter:
    def __init__(self, channel, *, result=None, raises=None):
        self.channel = channel
        self._result = result
        self._raises = raises
        self.calls = 0

    async def publish(self, content, *, publication_id):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._result or ChannelResult(self.channel, ResultStatus.DONE, external_id="ok")


def _patch_adapters(mapping):
    async def fake_build(repo, profile_id, channel, target_id="", *, store=None):
        return mapping.get(channel)

    return patch(_PATCH, new=fake_build)


async def _make_profile(factory, channels_live, *, name="p") -> int:
    from crosspost.channels.validators import VALIDATORS

    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile(name)
        for ch in channels_live:
            await repo.upsert_connection(p.id, ch, ConnectionState.LIVE)
            if VALIDATORS[ch].needs_target:
                await repo.set_credential(p.id, ch, CredentialKind.TARGET, f"t-{name}")
        return p.id


async def _seed_post(
    factory, profile_id, when, channels, *, media_key="sched-x", text="hi", article_text=None
):
    """Создать scheduled-пост + физический медиа-файл под MEDIA_DIR/<key>/."""
    import os

    media_dir = Path(os.environ["MEDIA_DIR"]) / media_key
    media_dir.mkdir(parents=True, exist_ok=True)
    media_file = media_dir / "00_photo.jpg"
    media_file.write_bytes(b"\xff\xd8\xff")

    async with factory() as s:
        repo = PublicationRepository(s, profile_id=profile_id)
        post = await repo.create_scheduled(
            content_type="post",
            text=text,
            title=None,
            media_paths=[str(media_file)],
            channels=channels,
            scheduled_at=when,
            article_text=article_text,
        )
        return post.id, media_dir


async def _post_status(factory, post_id):
    async with factory() as s:
        post = await SchedulerRepository(s).get(post_id)
        return None if post is None else str(post.status)


async def _channel_rows(factory, profile_id, post_id):
    async with factory() as s:
        repo = PublicationRepository(s, profile_id=profile_id)
        rows = await repo.list_statuses(scheduled_publication_id(post_id))
        return {r.channel: r for r in rows}


# ── Публикация созревшего поста ────────────────────────────────────────────────


async def test_due_post_is_published(factory):
    pid = await _make_profile(factory, ["telegram"], name="due")
    past = datetime.now() - timedelta(minutes=1)
    post_id, media_dir = await _seed_post(factory, pid, past, ["telegram"])

    adapter = _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    ex = ScheduledExecutor(factory, pacing_seconds=0, poll_seconds=0)
    with _patch_adapters({"telegram": adapter}):
        processed = await ex.run_once()

    assert processed == [post_id]
    assert adapter.calls == 1
    assert await _post_status(factory, post_id) == ScheduledPostStatus.DONE
    rows = await _channel_rows(factory, pid, post_id)
    assert str(rows["telegram"].status) == "done"
    assert rows["telegram"].external_id == "tg1"
    assert not media_dir.exists()  # медиа очищено после финала


async def test_future_post_is_left_alone(factory):
    pid = await _make_profile(factory, ["telegram"], name="fut")
    future = datetime.now() + timedelta(days=1)
    post_id, media_dir = await _seed_post(factory, pid, future, ["telegram"])

    adapter = _MockAdapter("telegram")
    ex = ScheduledExecutor(factory, pacing_seconds=0)
    with _patch_adapters({"telegram": adapter}):
        processed = await ex.run_once()

    assert processed == []
    assert adapter.calls == 0
    assert await _post_status(factory, post_id) == ScheduledPostStatus.SCHEDULED
    assert media_dir.exists()  # не тронуто


# ── Идемпотентность между «перезапусками» ──────────────────────────────────────


async def test_idempotent_across_reruns(factory):
    """Повторный проход (симуляция рестарта) не публикует канал второй раз."""
    pid = await _make_profile(factory, ["telegram"], name="idem")
    past = datetime.now() - timedelta(minutes=1)
    post_id, _ = await _seed_post(factory, pid, past, ["telegram"])

    adapter = _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "x"))
    ex = ScheduledExecutor(factory, pacing_seconds=0)
    with _patch_adapters({"telegram": adapter}):
        await ex.run_once()
        # Второй исполнитель (как после рестарта) — тот же publication_id.
        ex2 = ScheduledExecutor(factory, pacing_seconds=0)
        await ex2.run_once()

    assert adapter.calls == 1  # опубликовано ровно один раз


# ── Ретрай с потолком ──────────────────────────────────────────────────────────


async def test_retry_until_cap_then_failed(factory):
    pid = await _make_profile(factory, ["telegram"], name="cap")
    past = datetime.now() - timedelta(minutes=1)
    post_id, _ = await _seed_post(factory, pid, past, ["telegram"])

    adapter = _MockAdapter("telegram", raises=RuntimeError("канал упал"))
    ex = ScheduledExecutor(factory, max_attempts=3, pacing_seconds=0)
    with _patch_adapters({"telegram": adapter}):
        # Тик 1 и 2: пост остаётся scheduled (лимит не исчерпан).
        await ex.run_once()
        assert await _post_status(factory, post_id) == ScheduledPostStatus.SCHEDULED
        await ex.run_once()
        assert await _post_status(factory, post_id) == ScheduledPostStatus.SCHEDULED
        # Тик 3: достигнут потолок → терминально failed, пост закрыт.
        await ex.run_once()

    assert adapter.calls == 3
    assert await _post_status(factory, post_id) == ScheduledPostStatus.FAILED
    rows = await _channel_rows(factory, pid, post_id)
    assert str(rows["telegram"].status) == "failed"
    assert rows["telegram"].attempt_count == 3
    # Четвёртый проход уже ничего не делает (пост не scheduled).
    with _patch_adapters({"telegram": adapter}):
        assert await ex.run_once() == []
    assert adapter.calls == 3


# ── Частичный успех: медиа НЕ чистится, пока не финализированы все каналы ───────


async def test_media_kept_until_all_channels_final(factory):
    pid = await _make_profile(factory, ["telegram", "vk_wall"], name="partial")
    past = datetime.now() - timedelta(minutes=1)
    post_id, media_dir = await _seed_post(factory, pid, past, ["telegram", "vk_wall"])

    ok = _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "t"))
    bad = _MockAdapter("vk_wall", raises=RuntimeError("vk down"))
    ex = ScheduledExecutor(factory, max_attempts=2, pacing_seconds=0)
    with _patch_adapters({"telegram": ok, "vk_wall": bad}):
        await ex.run_once()  # telegram done, vk_wall failed (attempt 1) → не финал
        assert media_dir.exists()
        assert await _post_status(factory, post_id) == ScheduledPostStatus.SCHEDULED
        await ex.run_once()  # vk_wall attempt 2 → потолок → все терминальны

    assert await _post_status(factory, post_id) == ScheduledPostStatus.FAILED
    assert not media_dir.exists()  # теперь очищено
    # telegram НЕ публиковался повторно (был done).
    assert ok.calls == 1
    assert bad.calls == 2


# ── Пейсинг: несколько созревших постов идут с паузой ──────────────────────────


async def test_pacing_between_multiple_due_posts(factory):
    pid = await _make_profile(factory, ["telegram"], name="pace")
    past = datetime.now() - timedelta(minutes=1)
    id1, _ = await _seed_post(factory, pid, past, ["telegram"], media_key="sched-1")
    id2, _ = await _seed_post(factory, pid, past, ["telegram"], media_key="sched-2")

    sleeps: list[float] = []

    async def recording_sleep(seconds):
        sleeps.append(seconds)

    adapter = _MockAdapter("telegram")
    ex = ScheduledExecutor(factory, pacing_seconds=7, poll_seconds=0, sleep=recording_sleep)
    with _patch_adapters({"telegram": adapter}):
        processed = await ex.run_once()

    assert set(processed) == {id1, id2}
    # Между двумя постами ровно одна пауза пейсинга (перед первым — нет).
    assert sleeps == [7]


# ── Изоляция профиля ───────────────────────────────────────────────────────────


async def test_publish_recorded_under_correct_profile(factory):
    pa = await _make_profile(factory, ["telegram"], name="iso-a")
    pb = await _make_profile(factory, ["telegram"], name="iso-b")
    past = datetime.now() - timedelta(minutes=1)
    post_id, _ = await _seed_post(factory, pa, past, ["telegram"], media_key="sched-iso")

    adapter = _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "x"))
    ex = ScheduledExecutor(factory, pacing_seconds=0)
    with _patch_adapters({"telegram": adapter}):
        await ex.run_once()

    # Публикация видна под профилем A и НЕ видна под профилем B.
    rows_a = await _channel_rows(factory, pa, post_id)
    rows_b = await _channel_rows(factory, pb, post_id)
    assert "telegram" in rows_a
    assert rows_b == {}


# ── Диагностика: попытки пишутся в logs ────────────────────────────────────────


async def test_attempts_are_logged(factory):
    pid = await _make_profile(factory, ["telegram"], name="log")
    past = datetime.now() - timedelta(minutes=1)
    post_id, _ = await _seed_post(factory, pid, past, ["telegram"])

    adapter = _MockAdapter("telegram", raises=RuntimeError("boom"))
    ex = ScheduledExecutor(factory, max_attempts=1, pacing_seconds=0)
    with _patch_adapters({"telegram": adapter}):
        await ex.run_once()

    async with factory() as s:
        logs = await LogRepository(s).list_for_profile(pid)
    assert len(logs) == 1
    assert logs[0].channel == "telegram"
    assert logs[0].level == "ERROR"
    assert "boom" in logs[0].message
    assert logs[0].publication_id == scheduled_publication_id(post_id)


# ── Связка статья→канал (article_text) в планировщике ─────────────────────────


class _CapturingMock:
    """Мок адаптера, запоминающий text/type каждой публикации."""

    def __init__(self, channel, *, result=None, raises=None):
        self.channel = channel
        self._result = result
        self._raises = raises
        self.texts: list[str] = []
        self.types: list[str] = []
        self.calls = 0

    async def publish(self, content, *, publication_id):
        self.calls += 1
        self.texts.append(content.text)
        self.types.append(str(content.type))
        if self._raises:
            raise self._raises
        return self._result or ChannelResult(self.channel, ResultStatus.DONE, external_id="ok")


async def test_article_binding_telegraph_first_then_tg_with_link(factory):
    pid = await _make_profile(factory, ["telegram", "telegraph"], name="art")
    past = datetime.now() - timedelta(minutes=1)
    post_id, media_dir = await _seed_post(
        factory, pid, past, ["telegram"], article_text="Тело лонгрида"
    )
    url = "https://telegra.ph/Long-08-12"
    tgraph = _CapturingMock(
        "telegraph", result=ChannelResult("telegraph", ResultStatus.DONE, external_id=url)
    )
    tg = _CapturingMock("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    ex = ScheduledExecutor(factory, max_attempts=3, pacing_seconds=0)
    with _patch_adapters({"telegraph": tgraph, "telegram": tg}):
        await ex.run_once()
        # Идемпотентность: второй тик не создаёт вторую страницу.
        await ex.run_once()

    assert tgraph.calls == 1
    assert tgraph.types == ["article"]
    assert tgraph.texts == ["Тело лонгрида"]
    assert tg.calls == 1
    assert url in tg.texts[0]
    assert await _post_status(factory, post_id) == ScheduledPostStatus.DONE
    assert not media_dir.exists()


async def test_article_ceiling_fail_closed_channel_not_sent(factory):
    from crosspost.content.article_link import NO_ARTICLE_REASON

    pid = await _make_profile(factory, ["telegram", "telegraph"], name="artfail")
    past = datetime.now() - timedelta(minutes=1)
    post_id, _ = await _seed_post(factory, pid, past, ["telegram"], article_text="Тело")

    tgraph = _CapturingMock("telegraph", raises=RuntimeError("telegraph down"))
    tg = _CapturingMock("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    ex = ScheduledExecutor(factory, max_attempts=1, pacing_seconds=0)
    with _patch_adapters({"telegraph": tgraph, "telegram": tg}):
        await ex.run_once()  # telegraph падает до потолка (max=1) → fail-closed

    assert tg.calls == 0  # TG не публиковался
    assert await _post_status(factory, post_id) == ScheduledPostStatus.FAILED  # не крутится вечно
    rows = await _channel_rows(factory, pid, post_id)
    assert str(rows["telegram"].status) == "failed"
    assert rows["telegram"].error == NO_ARTICLE_REASON


async def test_article_pending_keeps_scheduled_and_tg_untouched(factory):
    pid = await _make_profile(factory, ["telegram", "telegraph"], name="artpend")
    past = datetime.now() - timedelta(minutes=1)
    post_id, _ = await _seed_post(factory, pid, past, ["telegram"], article_text="Тело")

    tgraph = _CapturingMock("telegraph", raises=RuntimeError("temporary"))
    tg = _CapturingMock("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    ex = ScheduledExecutor(factory, max_attempts=3, pacing_seconds=0)  # 1 попытка < потолка
    with _patch_adapters({"telegraph": tgraph, "telegram": tg}):
        await ex.run_once()

    assert tg.calls == 0  # статья ещё не готова → канал не трогаем
    assert (
        await _post_status(factory, post_id) == ScheduledPostStatus.SCHEDULED
    )  # добьёт следующий тик


# ── Мультицели: пост хранит и исполняет выбранные цели (Блок 4) ────────────────


def _patch_targets(mapping):
    """Фабрика по (channel, target_id) → adapter, плюс запись вызовов в calls."""
    calls: list[tuple[str, str]] = []

    async def fake_build(repo, profile_id, channel, target_id="", *, store=None):
        calls.append((channel, target_id))
        return mapping.get((channel, target_id))

    return patch(_PATCH, new=fake_build), calls


async def _target_rows(factory, profile_id, post_id):
    async with factory() as s:
        repo = PublicationRepository(s, profile_id=profile_id)
        rows = await repo.list_statuses(scheduled_publication_id(post_id))
        return {(r.channel, r.target): r for r in rows}


async def test_multi_target_post_publishes_each_target(factory):
    """Пост с двумя целями одного канала публикуется в каждую по отдельности."""
    pid = await _make_profile(factory, ["vk_wall"], name="mt")
    past = datetime.now() - timedelta(minutes=1)
    post_id, media_dir = await _seed_post(
        factory,
        pid,
        past,
        [{"channel": "vk_wall", "target_id": "10"}, {"channel": "vk_wall", "target_id": "20"}],
    )

    a = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "A"))
    b = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "B"))
    patcher, calls = _patch_targets({("vk_wall", "10"): a, ("vk_wall", "20"): b})
    ex = ScheduledExecutor(factory, pacing_seconds=0)
    with patcher:
        await ex.run_once()

    assert calls == [("vk_wall", "10"), ("vk_wall", "20")]
    assert a.calls == 1 and b.calls == 1
    rows = await _target_rows(factory, pid, post_id)
    assert str(rows[("vk_wall", "10")].status) == "done"
    assert rows[("vk_wall", "10")].external_id == "A"
    assert str(rows[("vk_wall", "20")].status) == "done"
    assert rows[("vk_wall", "20")].external_id == "B"
    assert await _post_status(factory, post_id) == ScheduledPostStatus.DONE
    assert not media_dir.exists()


async def test_pacing_between_targets_within_post(factory):
    """Между отправками в разные цели одного поста — пауза пейсинга."""
    pid = await _make_profile(factory, ["vk_wall"], name="mtpace")
    past = datetime.now() - timedelta(minutes=1)
    await _seed_post(
        factory,
        pid,
        past,
        [{"channel": "vk_wall", "target_id": "10"}, {"channel": "vk_wall", "target_id": "20"}],
    )

    sleeps: list[float] = []

    async def recording_sleep(seconds):
        sleeps.append(seconds)

    a = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "A"))
    b = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "B"))
    patcher, _ = _patch_targets({("vk_wall", "10"): a, ("vk_wall", "20"): b})
    ex = ScheduledExecutor(factory, pacing_seconds=4, poll_seconds=0, sleep=recording_sleep)
    with patcher:
        await ex.run_once()

    # Ровно одна пауза между двумя целями (перед первой — нет).
    assert sleeps == [4]


async def test_multi_target_idempotent_across_reruns(factory):
    """Второй тик не публикует повторно в уже-done цель."""
    pid = await _make_profile(factory, ["vk_wall"], name="mtidem")
    past = datetime.now() - timedelta(minutes=1)
    await _seed_post(
        factory,
        pid,
        past,
        [{"channel": "vk_wall", "target_id": "10"}, {"channel": "vk_wall", "target_id": "20"}],
    )

    a = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "A"))
    b = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "B"))
    patcher, _ = _patch_targets({("vk_wall", "10"): a, ("vk_wall", "20"): b})
    ex = ScheduledExecutor(factory, pacing_seconds=0)
    with patcher:
        await ex.run_once()
        await ex.run_once()  # рестарт-симуляция

    assert a.calls == 1 and b.calls == 1


async def test_multi_target_partial_success(factory):
    """Одна цель падает до потолка, другая проходит — пост финализируется."""
    pid = await _make_profile(factory, ["vk_wall"], name="mtpart")
    past = datetime.now() - timedelta(minutes=1)
    post_id, media_dir = await _seed_post(
        factory,
        pid,
        past,
        [{"channel": "vk_wall", "target_id": "10"}, {"channel": "vk_wall", "target_id": "20"}],
    )

    ok = _MockAdapter("vk_wall", result=ChannelResult("vk_wall", ResultStatus.DONE, "A"))
    bad = _MockAdapter("vk_wall", raises=RuntimeError("target 20 down"))
    patcher, _ = _patch_targets({("vk_wall", "10"): ok, ("vk_wall", "20"): bad})
    ex = ScheduledExecutor(factory, max_attempts=1, pacing_seconds=0)
    with patcher:
        await ex.run_once()

    rows = await _target_rows(factory, pid, post_id)
    assert str(rows[("vk_wall", "10")].status) == "done"
    assert str(rows[("vk_wall", "20")].status) == "failed"
    assert await _post_status(factory, post_id) == ScheduledPostStatus.FAILED
    assert not media_dir.exists()
    assert ok.calls == 1  # успешная цель не пересылалась


# ── Статусный автомат: атомарный клейм, реконсиляция, миграция ─────────────────


async def test_concurrent_claim_only_one_wins(factory):
    """Два клейма одного SCHEDULED-поста → ровно один rowcount=1, второй пропускает."""
    pid = await _make_profile(factory, ["telegram"], name="claim")
    past = datetime.now() - timedelta(minutes=1)
    post_id, _ = await _seed_post(factory, pid, past, ["telegram"])

    async with factory() as s1, factory() as s2:
        won1 = await SchedulerRepository(s1).claim(post_id)
        won2 = await SchedulerRepository(s2).claim(post_id)
    assert [won1, won2].count(True) == 1
    assert await _post_status(factory, post_id) == ScheduledPostStatus.PUBLISHING


async def test_claim_non_scheduled_fails(factory):
    """Клейм не-SCHEDULED поста (уже PUBLISHING/DONE/FAILED) → rowcount=0."""
    pid = await _make_profile(factory, ["telegram"], name="claim2")
    past = datetime.now() - timedelta(minutes=1)
    post_id, _ = await _seed_post(factory, pid, past, ["telegram"])

    async with factory() as s:
        repo = SchedulerRepository(s)
        assert await repo.claim(post_id) is True  # первый клейм — PUBLISHING
        assert await repo.claim(post_id) is False  # повторный — уже не scheduled
        await repo.finalize(post_id, success=True)
        assert await repo.claim(post_id) is False  # DONE — тоже не заклеймить


async def test_finalize_done_and_failed(factory):
    """finalize раскладывает по success в DONE / FAILED (из PUBLISHING)."""
    pid = await _make_profile(factory, ["telegram"], name="fin")
    past = datetime.now() - timedelta(minutes=1)
    ok_id, _ = await _seed_post(factory, pid, past, ["telegram"], media_key="fin-ok")
    bad_id, _ = await _seed_post(factory, pid, past, ["telegram"], media_key="fin-bad")

    async with factory() as s:
        repo = SchedulerRepository(s)
        await repo.claim(ok_id)
        await repo.finalize(ok_id, success=True)
        await repo.claim(bad_id)
        await repo.finalize(bad_id, success=False)
    assert await _post_status(factory, ok_id) == ScheduledPostStatus.DONE
    assert await _post_status(factory, bad_id) == ScheduledPostStatus.FAILED


async def test_reconcile_resets_stuck_publishing(factory):
    """PUBLISHING (процесс умер в полёте) на старте → реконсиляция в SCHEDULED."""
    pid = await _make_profile(factory, ["telegram"], name="stuck")
    past = datetime.now() - timedelta(minutes=1)
    post_id, _ = await _seed_post(factory, pid, past, ["telegram"])

    async with factory() as s:
        repo = SchedulerRepository(s)
        await repo.claim(post_id)  # застряли в PUBLISHING
        assert await _post_status(factory, post_id) == ScheduledPostStatus.PUBLISHING
        n = await repo.reconcile_stuck()
    assert n == 1
    assert await _post_status(factory, post_id) == ScheduledPostStatus.SCHEDULED


async def test_legacy_published_migrates_to_done_or_failed(factory):
    """Легаси status='published' раскладывается миграцией в DONE/FAILED по публикациям."""
    from sqlalchemy import text

    pid = await _make_profile(factory, ["telegram"], name="mig")
    past = datetime.now() - timedelta(minutes=1)
    done_id, _ = await _seed_post(factory, pid, past, ["telegram"], media_key="mig-d")
    fail_id, _ = await _seed_post(factory, pid, past, ["telegram"], media_key="mig-f")

    async with factory() as s:
        pubrepo = PublicationRepository(s, profile_id=pid)
        await pubrepo.set_status(
            scheduled_publication_id(done_id), "telegram", PublicationStatus.DONE, external_id="x"
        )
        await pubrepo.set_status(
            scheduled_publication_id(fail_id), "telegram", PublicationStatus.FAILED, error="boom"
        )
        # Симулируем легаси-строки: оба в старом статусе 'published'.
        for i in (done_id, fail_id):
            await s.execute(
                text("UPDATE scheduled_posts SET status='published' WHERE id=:i"), {"i": i}
            )
        await s.commit()
        # Прогоняем миграцию напрямую (как в _migrate_sqlite на старте движка).
        conn = await s.connection()
        await conn.run_sync(_migrate_scheduled_status)
        await s.commit()

    assert await _post_status(factory, done_id) == ScheduledPostStatus.DONE
    assert await _post_status(factory, fail_id) == ScheduledPostStatus.FAILED


async def test_legacy_list_str_shape_still_executes(factory):
    """Старый JSON-shape channels=list[str] исполняется как таргетлесс единицы."""
    pid = await _make_profile(factory, ["telegram"], name="legacy")
    past = datetime.now() - timedelta(minutes=1)
    post_id, _ = await _seed_post(factory, pid, past, ["telegram"])  # старый формат

    a = _MockAdapter("telegram", result=ChannelResult("telegram", ResultStatus.DONE, "tg1"))
    patcher, calls = _patch_targets({("telegram", ""): a})
    ex = ScheduledExecutor(factory, pacing_seconds=0)
    with patcher:
        await ex.run_once()

    assert calls == [("telegram", "")]
    rows = await _target_rows(factory, pid, post_id)
    assert str(rows[("telegram", "")].status) == "done"
    assert await _post_status(factory, post_id) == ScheduledPostStatus.DONE
