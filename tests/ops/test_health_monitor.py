"""T-304: deterministic scheduler readiness and external alert transitions."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from crosspost.db.engine import create_engine_and_tables
from crosspost.ops.health import (
    ErrorSpikeCache,
    HealthConfig,
    SchedulerProgress,
    evaluate_readiness,
)
from crosspost.ops.monitor import (
    MonitorConfig,
    probe_health,
    run_monitor_once,
    send_telegram_alert,
)
from crosspost.web.routes import health


def test_scheduler_progress_is_monotonic_and_fails_closed_on_stall() -> None:
    now = [100.0]
    progress = SchedulerProgress(clock=lambda: now[0])
    assert not progress.healthy(max_silence_seconds=20, max_consecutive_errors=3)
    progress.pulse()  # starting a tick is not proof of a completed iteration
    assert not progress.healthy(max_silence_seconds=20, max_consecutive_errors=3)
    progress.succeeded()
    assert progress.healthy(max_silence_seconds=20, max_consecutive_errors=3)
    progress.failed()
    progress.failed()
    assert progress.healthy(max_silence_seconds=20, max_consecutive_errors=3)
    progress.failed()
    assert not progress.healthy(max_silence_seconds=20, max_consecutive_errors=3)
    progress.succeeded()
    assert progress.healthy(max_silence_seconds=20, max_consecutive_errors=3)
    now[0] += 21
    assert not progress.healthy(max_silence_seconds=20, max_consecutive_errors=3)
    progress.pulse()
    assert progress.healthy(max_silence_seconds=20, max_consecutive_errors=3)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1.0, 0.0])
def test_scheduler_timeout_rejects_unbounded_values(value: float) -> None:
    with pytest.raises(ValueError):
        HealthConfig(max_silence_seconds=value)


@pytest.fixture()
async def db_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_readiness_db_scheduler_and_error_spike(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    progress = SchedulerProgress(clock=lambda: 42.0)
    progress.succeeded()
    task = asyncio.create_task(asyncio.sleep(3600))
    config = HealthConfig(max_silence_seconds=30, max_consecutive_errors=3, error_threshold=1)
    try:
        good = await evaluate_readiness(
            db_factory,
            scheduler_enabled=True,
            scheduler_task=task,
            progress=progress,
            config=config,
        )
        assert good.ready and not good.error_spike

        from crosspost.db.models import Log, Profile

        async with db_factory() as session:
            profile = Profile(name="profile")
            session.add(profile)
            await session.flush()
            session.add(Log(profile_id=profile.id, level="ERROR", message="test"))
            await session.commit()
        spiked = await evaluate_readiness(
            db_factory,
            scheduler_enabled=True,
            scheduler_task=task,
            progress=progress,
            config=config,
            now=lambda: datetime.now(UTC),
        )
        assert spiked.ready and spiked.error_spike

        older = await evaluate_readiness(
            db_factory,
            scheduler_enabled=True,
            scheduler_task=task,
            progress=progress,
            config=config,
            now=lambda: datetime.now(UTC) + timedelta(days=1),
        )
        assert older.ready and not older.error_spike

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        dead = await evaluate_readiness(
            db_factory,
            scheduler_enabled=True,
            scheduler_task=task,
            progress=progress,
            config=config,
        )
        assert not dead.ready

        unknown = await evaluate_readiness(
            db_factory, scheduler_enabled=None, scheduler_task=None, progress=None, config=config
        )
        assert not unknown.ready
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_health_endpoints_no_auth_no_details(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = FastAPI()
    app.include_router(health.router)
    app.state.health_factory = db_factory
    app.state.scheduler_enabled = True
    app.state.scheduler_task = asyncio.create_task(asyncio.sleep(3600))
    app.state.scheduler_progress = SchedulerProgress()
    app.state.scheduler_progress.succeeded()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            live = await client.get("/health/live")
            assert live.status_code == 200
            good = await client.get("/health")
            assert good.status_code == 200
            assert good.json() == {"status": "ok", "error_spike": False}
            assert good.headers["Cache-Control"] == "no-store"
            app.state.scheduler_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await app.state.scheduler_task
            dead = await client.get("/health")
            assert dead.status_code == 503
            assert dead.json() == {"status": "unavailable", "error_spike": False}
            assert await client.get("/health/live") != dead
    finally:
        if not app.state.scheduler_task.done():
            app.state.scheduler_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await app.state.scheduler_task


async def test_health_db_failure_is_503() -> None:
    app = FastAPI()
    app.include_router(health.router)
    app.state.health_factory = None
    app.state.scheduler_enabled = True
    app.state.scheduler_task = asyncio.create_task(asyncio.sleep(3600))
    app.state.scheduler_progress = SchedulerProgress()
    app.state.scheduler_progress.succeeded()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
            assert response.status_code == 503
            assert response.json() == {"status": "unavailable", "error_spike": False}
    finally:
        app.state.scheduler_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await app.state.scheduler_task


def test_external_monitor_deduplicates_and_retries_failed_notification(tmp_path) -> None:
    state = tmp_path / "monitor.state"
    cfg = MonitorConfig("https://app.example.test/health", state)
    messages: list[str] = []

    def send(message: str) -> None:
        messages.append(message)

    assert run_monitor_once(cfg, probe=lambda _: "up", notify=send) == "up"
    assert messages == []
    assert run_monitor_once(cfg, probe=lambda _: "down", notify=send) == "down"
    assert messages == ["down"]
    assert run_monitor_once(cfg, probe=lambda _: "down", notify=send) == "down"
    assert messages == ["down"]
    assert run_monitor_once(cfg, probe=lambda _: "degraded", notify=send) == "degraded"
    assert messages == ["down", "degraded"]

    def fail(_message: str) -> None:
        raise RuntimeError("notification failure")

    with pytest.raises(RuntimeError):
        run_monitor_once(cfg, probe=lambda _: "up", notify=fail)
    assert state.read_text() == "degraded"
    assert run_monitor_once(cfg, probe=lambda _: "up", notify=send) == "up"
    assert messages == ["down", "degraded", "up"]


def test_monitor_rejects_insecure_public_url(tmp_path) -> None:
    with pytest.raises(ValueError):
        MonitorConfig("http://public.example.test/health", tmp_path / "state")


async def test_health_real_db_failure_is_503() -> None:
    class BadFactory:
        def __call__(self) -> None:
            raise RuntimeError("DB_URL and private credentials must never be shown")

    app = FastAPI()
    app.include_router(health.router)
    app.state.health_factory = BadFactory()
    app.state.scheduler_enabled = True
    app.state.scheduler_task = asyncio.create_task(asyncio.sleep(3600))
    app.state.scheduler_progress = SchedulerProgress()
    app.state.scheduler_progress.succeeded()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
            assert response.status_code == 503
            assert "DB_URL" not in response.text
            assert response.json() == {"status": "unavailable", "error_spike": False}
    finally:
        app.state.scheduler_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await app.state.scheduler_task


async def test_db_without_crosspost_schema_is_not_ready() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    task = asyncio.create_task(asyncio.sleep(3600))
    try:
        result = await evaluate_readiness(
            factory,
            scheduler_enabled=True,
            scheduler_task=task,
            progress=SchedulerProgress(successful_ticks=1),
            config=HealthConfig(error_threshold=0),
        )
        assert not result.ready
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await engine.dispose()


def test_probe_recognizes_degradation_without_transport_error(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size: int) -> bytes:
            return json.dumps({"status": "ok", "error_spike": True}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert probe_health("https://app.example.test/health") == "degraded"

    def fail(*_args, **_kwargs):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    assert probe_health("https://app.example.test/health") == "down"


def test_telegram_transport_retries_and_never_leaks_token(monkeypatch, capsys) -> None:
    attempts = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size: int) -> bytes:
            return b'{"ok":true}'

    def flaky(request, **_kwargs):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise urllib.error.URLError("SENSITIVE_TOKEN network failure")
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    monkeypatch.setattr("crosspost.ops.monitor.time.sleep", lambda _: None)
    send_telegram_alert(token="SENSITIVE_TOKEN", chat_id="123", state="down")
    assert len(attempts) == 3
    assert "SENSITIVE_TOKEN" not in capsys.readouterr().err


async def test_scheduler_first_tick_and_exit_during_db_wait_are_not_ready(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    progress = SchedulerProgress(clock=lambda: 42.0)
    task = asyncio.create_task(asyncio.sleep(3600))
    config = HealthConfig(error_threshold=0)
    try:
        assert not (
            await evaluate_readiness(
                db_factory,
                scheduler_enabled=True,
                scheduler_task=task,
                progress=progress,
                config=config,
            )
        ).ready
        progress.pulse()
        assert not (
            await evaluate_readiness(
                db_factory,
                scheduler_enabled=True,
                scheduler_task=task,
                progress=progress,
                config=config,
            )
        ).ready
        progress.succeeded()

        class CancelOnExecute:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def execute(self, _statement):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        class CancelFactory:
            def __call__(self):
                return CancelOnExecute()

        result = await evaluate_readiness(
            CancelFactory(),  # type: ignore[arg-type]
            scheduler_enabled=True,
            scheduler_task=task,
            progress=progress,
            config=config,
        )
        assert not result.ready
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_diagnostics_are_indexed_cached_and_nonfatal(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    from crosspost.db.models import Log, Profile

    now = [20.0]
    cache = ErrorSpikeCache(clock=lambda: now[0])
    progress = SchedulerProgress()
    progress.succeeded()
    task = asyncio.create_task(asyncio.sleep(3600))
    engine = db_factory.kw["bind"]
    statements: list[str] = []

    def audit(_conn, _cursor, statement, _parameters, _context, _many):
        if "SELECT count(*)" in statement:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", audit)
    try:
        async with db_factory() as session:
            profile = Profile(name="profile")
            session.add(profile)
            await session.flush()
            session.add(Log(profile_id=profile.id, level="ERROR", message="failure"))
            await session.commit()

        config = HealthConfig(error_threshold=1, error_refresh_seconds=60)

        async def check() -> bool:
            result = await evaluate_readiness(
                db_factory,
                scheduler_enabled=True,
                scheduler_task=task,
                progress=progress,
                config=config,
                error_cache=cache,
            )
            assert result.ready
            return result.error_spike

        assert await check()
        assert await check()
        assert len(statements) == 1  # multiple anonymous calls cannot multiply scans
        now[0] += 61
        assert await check()
        assert len(statements) == 2
        assert "LIMIT" in statements[0]

        async with engine.connect() as conn:
            plan = await conn.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM "
                    "(SELECT id FROM logs WHERE level = 'ERROR' "
                    "AND created_at >= '2026-01-01' LIMIT 10)"
                )
            )
            assert any("USING COVERING INDEX ix_logs_level_created_at" in row[3] for row in plan)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", audit)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_diagnostic_failure_degrades_without_restarting(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = db_factory.kw["bind"]
    progress = SchedulerProgress()
    progress.succeeded()
    task = asyncio.create_task(asyncio.sleep(3600))

    def fail_diagnostic(_conn, _cursor, statement, _parameters, _context, _many):
        if "SELECT count(*)" in statement:
            raise RuntimeError("diagnostic-error private SQL")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_diagnostic)
    try:
        result = await evaluate_readiness(
            db_factory,
            scheduler_enabled=True,
            scheduler_task=task,
            progress=progress,
            config=HealthConfig(error_threshold=1),
        )
        assert result.ready
        assert result.error_spike  # fail visibly degraded; core DB remains working
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_diagnostic)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_external_probe_rejects_malformed_payload_and_truncated_read(monkeypatch) -> None:
    class Response:
        status = 200

        def __init__(self, value: bytes | None = None):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            if self.value is None:
                raise http.client.IncompleteRead(b"partial")
            return self.value

    for value in (b'{"status":"ok"}', b'{"status":"ok","error_spike":"true"}', b"[]", None):
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda *_a, _value=value, **_kw: Response(_value)
        )
        assert probe_health("https://app.example.test/health") == "down"


def test_telegram_retries_truncated_or_malformed_json(monkeypatch, capsys) -> None:
    attempts = [0]

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            if attempts[0] == 2:
                raise http.client.IncompleteRead(b"partial")
            return b"[]"

    def malformed(*_args, **_kwargs):
        attempts[0] += 1
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", malformed)
    monkeypatch.setattr("crosspost.ops.monitor.time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="could not be delivered"):
        send_telegram_alert(token="SENSITIVE_TOKEN", chat_id="123", state="down")
    assert attempts[0] == 3
    assert "SENSITIVE_TOKEN" not in capsys.readouterr().err
