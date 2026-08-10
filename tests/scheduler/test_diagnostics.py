"""Тесты диагностики (Блок 4): скриншот браузерного сбоя доезжает до logs.

Без реального браузера: capture_failure_screenshot тестируем на фейковой page;
проброс screenshot_path — на моках адаптеров через PublishService и executor.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.log_repo import LogRepository
from crosspost.db.models import ConnectionState
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.vault import get_vault
from crosspost.scheduler.executor import ScheduledExecutor


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCREENSHOT_DIR", str(tmp_path / "shots"))


# ── capture_failure_screenshot (юнит, фейковая page) ───────────────────────────


class _FakePage:
    def __init__(self, fail=False):
        self._fail = fail

    async def screenshot(self, *, path):
        if self._fail:
            raise RuntimeError("no page")
        Path(path).write_bytes(b"PNG")


async def test_capture_screenshot_writes_file(tmp_path, monkeypatch):
    from crosspost.adapters.browser.base_browser import capture_failure_screenshot

    path = await capture_failure_screenshot(_FakePage(), "yandex")
    assert path is not None
    assert Path(path).exists()
    assert "yandex-" in Path(path).name


async def test_capture_screenshot_never_raises(monkeypatch):
    from crosspost.adapters.browser.base_browser import capture_failure_screenshot

    # Даже если page.screenshot падает — возвращаем None, не пробрасываем.
    path = await capture_failure_screenshot(_FakePage(fail=True), "vk_wall")
    assert path is None


# ── Проброс через PublishService ───────────────────────────────────────────────


@pytest_asyncio.fixture
async def factory():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    f: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


class _ExcWithShot(RuntimeError):
    pass


class _FailingAdapter:
    channel = "vk_wall"

    async def publish(self, content, *, publication_id):
        exc = _ExcWithShot("vk упал")
        exc.screenshot_path = "/tmp/shots/vk_wall-abc.png"
        raise exc


async def test_publish_service_surfaces_screenshot_from_exception(factory):
    from crosspost.orchestrator.publish_service import PublishService

    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile("shot")
        pid = p.id
        pub_repo = PublicationRepository(s, profile_id=pid)

        async def build(channel):
            return _FailingAdapter()

        svc = PublishService(pub_repo, build)
        from crosspost.content.canonical import CanonicalContent, ContentType

        content = CanonicalContent(type=ContentType.POST, text="x", media_paths=[Path("a.jpg")])
        outcome = await svc.retry_channel(content, "vk_wall", publication_id="pub-1")

    assert outcome.status == "failed"
    assert outcome.screenshot_path == "/tmp/shots/vk_wall-abc.png"


# ── Скриншот попадает в Log через executor ─────────────────────────────────────


async def _make_profile(factory, channels_live, *, name="p"):
    from crosspost.channels.validators import VALIDATORS
    from crosspost.db.models import CredentialKind

    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        p = await repo.create_profile(name)
        for ch in channels_live:
            await repo.upsert_connection(p.id, ch, ConnectionState.LIVE)
            if VALIDATORS[ch].needs_target:
                await repo.set_credential(p.id, ch, CredentialKind.TARGET, "t")
        return p.id


async def _seed_post(factory, profile_id, when, channels):
    import os

    media_dir = Path(os.environ["MEDIA_DIR"]) / "sched-shot"
    media_dir.mkdir(parents=True, exist_ok=True)
    f = media_dir / "00_p.jpg"
    f.write_bytes(b"\xff")
    async with factory() as s:
        repo = PublicationRepository(s, profile_id=profile_id)
        post = await repo.create_scheduled(
            content_type="post",
            text="hi",
            title=None,
            media_paths=[str(f)],
            channels=channels,
            scheduled_at=when,
        )
        return post.id


async def test_executor_logs_screenshot_path(factory):
    pid = await _make_profile(factory, ["vk_wall"], name="exec-shot")
    past = datetime.now() - timedelta(minutes=1)
    await _seed_post(factory, pid, past, ["vk_wall"])

    async def fake_build(repo, profile_id, channel, *, store=None):
        return _FailingAdapter()

    ex = ScheduledExecutor(factory, max_attempts=1, pacing_seconds=0)
    with patch("crosspost.scheduler.executor.build_profile_adapter", new=fake_build):
        await ex.run_once()

    async with factory() as s:
        logs = await LogRepository(s).list_for_profile(pid)
    assert len(logs) == 1
    assert logs[0].level == "ERROR"
    assert logs[0].screenshot_path == "/tmp/shots/vk_wall-abc.png"
