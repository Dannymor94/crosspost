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
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.log_repo import LogRepository
from crosspost.db.models import ConnectionState, CredentialKind, ScheduledPostStatus
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
    async def fake_build(repo, profile_id, channel, *, store=None):
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
    assert await _post_status(factory, post_id) == ScheduledPostStatus.PUBLISHED
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
    assert await _post_status(factory, post_id) == ScheduledPostStatus.PUBLISHED
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

    assert await _post_status(factory, post_id) == ScheduledPostStatus.PUBLISHED
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
    assert await _post_status(factory, post_id) == ScheduledPostStatus.PUBLISHED
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
    assert (
        await _post_status(factory, post_id) == ScheduledPostStatus.PUBLISHED
    )  # не крутится вечно
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
