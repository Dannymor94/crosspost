"""Frozen CB-01..CB-18: real dispatch with mocked platform boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from crosspost.adapters.base import ChannelResult, FailureKind, ResultStatus
from crosspost.channels.validators import VALIDATORS
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import CircuitBreaker, ConnectionState, PublicationStatus
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.scheduler_repo import SchedulerRepository
from crosspost.db.vault import get_vault
from crosspost.orchestrator.breaker import BreakerPolicy, CircuitBreakerRepository
from crosspost.orchestrator.publish_service import PublishService, Selection
from crosspost.scheduler.executor import ScheduledExecutor, scheduled_publication_id

T0 = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = BreakerPolicy(cooldown_seconds=30, lease_seconds=60)
CONTENT = CanonicalContent(type=ContentType.POST, text="hello", media_paths=[Path("mock.jpg")])


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    url = f"sqlite+aiosqlite:///{tmp_path}/db.sqlite"
    engine = await create_engine_and_tables(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        repo = ProfileRepository(s, vault=get_vault())
        for name in ["one", "two"]:
            p = await repo.create_profile(name)
            for ch in ["telegram", "vk_wall", "vk_channel"]:
                await repo.upsert_connection(p.id, ch, ConnectionState.LIVE)
    yield factory, url
    await engine.dispose()


def adapter(status=ResultStatus.DONE, failure=FailureKind.UNKNOWN, retry_after=None):
    return AsyncMock(
        publish=AsyncMock(
            return_value=ChannelResult(
                "telegram",
                status,
                external_id="receipt" if status == ResultStatus.DONE else None,
                failure_kind=failure,
                retry_after=retry_after,
            )
        )
    )


def service(s, a, clock=lambda: T0, pid=1):
    factory = AsyncMock(return_value=a)
    return PublishService(
        PublicationRepository(s, profile_id=pid), factory, clock=clock, breaker_policy=POLICY
    ), factory


async def trip(factory, channel="telegram", pid=1):
    async with factory() as s:
        r = CircuitBreakerRepository(s)
        permit = await r.acquire(pid, channel, now=T0, policy=POLICY)
        assert permit
        await r.settle(pid, channel, permit, failure=FailureKind.RATE_LIMIT, now=T0, policy=POLICY)


async def snapshot(factory, channel="telegram", pid=1):
    async with factory() as s:
        return (
            await s.execute(
                select(CircuitBreaker).where(
                    CircuitBreaker.profile_id == pid, CircuitBreaker.channel == channel
                )
            )
        ).scalar_one()


async def test_cb01_open_immediate(db):
    f, _ = db
    await trip(f)
    async with f() as s:
        a = adapter()
        svc, build = service(s, a)
        out = await svc.publish(CONTENT, ["telegram"], publication_id="p")
        assert out[0].status == "deferred"
        build.assert_not_called()
        a.publish.assert_not_called()
        assert await svc._repo.get_status("p", "telegram") is None


async def test_cb02_open_scheduled_pending_other_channel_and_media(db, tmp_path):
    f, _ = db
    await trip(f)
    media = tmp_path / "keep" / "photo.jpg"
    media.parent.mkdir()
    media.write_bytes(b"media")
    async with f() as s:
        post = await PublicationRepository(s, profile_id=1).create_scheduled(
            content_type="post",
            text="hello",
            title=None,
            media_paths=[str(media)],
            channels=["telegram", "vk_wall"],
            scheduled_at=T0.replace(tzinfo=None),
        )
        post_id = post.id
    a = adapter()
    with patch(
        "crosspost.scheduler.executor.build_profile_adapter", AsyncMock(return_value=a)
    ) as build:
        await ScheduledExecutor(f, pacing_seconds=0, clock=lambda: T0).run_once(now=T0)
    assert [c.args[2] for c in build.call_args_list] == ["vk_wall"]
    async with f() as s:
        assert str((await SchedulerRepository(s).get(post_id)).status) == "scheduled"
        repo = PublicationRepository(s, profile_id=1)
        assert await repo.get_status(scheduled_publication_id(post_id), "telegram") is None
        assert (
            await repo.get_status(scheduled_publication_id(post_id), "vk_wall")
        ).attempt_count == 1
    assert media.read_bytes() == b"media"


async def test_cb03_open_manual_preserves_failed_row(db):
    f, _ = db
    await trip(f)
    async with f() as s:
        svc, build = service(s, adapter())
        await svc._repo.bump_attempt("p", "telegram")
        await svc._repo.set_status("p", "telegram", PublicationStatus.FAILED, error="old")
        out = await svc.retry_channel(CONTENT, "telegram", publication_id="p")
        assert out.status == "deferred"
        row = await svc._repo.get_status("p", "telegram")
        assert (str(row.status), row.attempt_count, row.error) == ("failed", 1, "old")
        build.assert_not_called()


@pytest.mark.parametrize("state", [ConnectionState.BANNED, ConnectionState.NEEDS_RELOGIN])
@pytest.mark.parametrize("open_breaker", [False, True])
async def test_cb04_connection_guard_before_breaker(db, state, open_breaker):
    f, _ = db
    if open_breaker:
        await trip(f)
    async with f() as s:
        await ProfileRepository(s, vault=get_vault()).upsert_connection(1, "telegram", state)
        svc, build = service(s, adapter(), clock=lambda: T0 + timedelta(days=10))
        out = await svc.retry_channel(CONTENT, "telegram", publication_id="p")
        assert out.status == str(state)
        build.assert_not_called()
        assert await svc._repo.get_status("p", "telegram") is None


async def test_cb05_rate_limit_stops_multitarget_with_bounded_retry_after(db):
    f, _ = db
    async with f() as s:
        a = adapter(ResultStatus.FAILED, FailureKind.RATE_LIMIT, 10**20)
        svc, build = service(s, a)
        out = await svc.publish(
            CONTENT, [Selection("telegram", "a"), Selection("telegram", "b")], publication_id="p"
        )
        assert [o.status for o in out] == ["failed", "deferred"]
        assert build.call_count == 1
    row = await snapshot(f)
    assert row.open_until == (T0 + timedelta(seconds=86400)).replace(tzinfo=None)


async def test_cb06_confirmed_ban_requires_verified_recovery(db):
    f, _ = db
    async with f() as s:
        a = adapter(ResultStatus.FAILED, FailureKind.BANNED)
        svc, _ = service(s, a)
        await svc.publish(CONTENT, ["telegram"], publication_id="p")
        later, build = service(s, adapter(), clock=lambda: T0 + timedelta(days=100))
        assert (
            await later.retry_channel(CONTENT, "telegram", publication_id="p")
        ).status == "banned"
        build.assert_not_called()
        # Existing explicit verification path: platform validator confirms the human recovery.
        from crosspost.channels.connection_validator import validate_connection

        with patch(
            "crosspost.channels.connection_validator.VALIDATORS",
            {"telegram": replace(VALIDATORS["telegram"], fn=AsyncMock(return_value=True))},
        ):
            await validate_connection(ProfileRepository(s, vault=get_vault()), 1, "telegram")
        assert (await later.retry_channel(CONTENT, "telegram", publication_id="p")).status == "done"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ProxyError("FAKE_SECRET"),
        httpx.ReadTimeout("FAKE_SECRET"),
        RuntimeError("banned 429 FAKE_SECRET"),
    ],
)
async def test_cb07_transport_unknown_never_ban(db, exc):
    f, _ = db
    async with f() as s:
        a = adapter()
        a.publish.side_effect = exc
        svc, _ = service(s, a)
        await svc.publish(CONTENT, ["telegram"], publication_id="p")
        conn = await ProfileRepository(s, vault=get_vault()).get_connection(1, "telegram")
        assert str(conn.state) == "live"
    assert str((await snapshot(f)).state) == "closed"


async def test_cb08_two_dispatchers_one_half_open_probe(db):
    f, _ = db
    await trip(f)
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kw):
        entered.set()
        await release.wait()
        return ChannelResult("telegram", ResultStatus.DONE, "ok")

    a = adapter()
    a.publish.side_effect = send
    async with f() as s1, f() as s2:
        svc1, _ = service(s1, a, clock=lambda: T0 + timedelta(seconds=30))
        svc2, b2 = service(s2, adapter(), clock=lambda: T0 + timedelta(seconds=30))
        task = asyncio.create_task(svc1.retry_channel(CONTENT, "telegram", publication_id="a"))
        await entered.wait()
        try:
            assert (
                await svc2.retry_channel(CONTENT, "telegram", publication_id="b")
            ).status == "deferred"
            b2.assert_not_called()
        finally:
            release.set()
            await task
    assert a.publish.call_count == 1


async def test_cb09_expired_hanging_probe_is_quarantined(db):
    f, _ = db
    await trip(f)
    entered, release = asyncio.Event(), asyncio.Event()
    moment = [T0 + timedelta(seconds=30)]

    async def send(*args, **kw):
        entered.set()
        await release.wait()
        return ChannelResult("telegram", ResultStatus.DONE, "late-receipt")

    a = adapter()
    a.publish.side_effect = send
    async with f() as s1, f() as s2:
        svc1, _ = service(s1, a, clock=lambda: moment[0])
        task = asyncio.create_task(
            svc1.retry_channel(CONTENT, "telegram", publication_id="hanging")
        )
        await entered.wait()
        try:
            moment[0] += timedelta(days=1)
            svc2, build = service(s2, adapter(), clock=lambda: moment[0])
            assert (
                await svc2.retry_channel(CONTENT, "telegram", publication_id="second")
            ).status == "deferred"
            build.assert_not_called()
            assert (await snapshot(f)).open_until is None
        finally:
            release.set()
            await task
    # The old receipt may finalize its publication, but cannot clear quarantine.
    assert (await snapshot(f)).open_until is None
    assert str((await snapshot(f)).state) == "open"
    assert a.publish.call_count == 1


@pytest.mark.parametrize("late_failure", [FailureKind.UNKNOWN, FailureKind.RATE_LIMIT])
async def test_cb10_late_result_cannot_clear_new_epoch(db, late_failure):
    f, _ = db
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kw):
        entered.set()
        await release.wait()
        return ChannelResult(
            "telegram",
            ResultStatus.DONE if late_failure == FailureKind.UNKNOWN else ResultStatus.FAILED,
            failure_kind=late_failure,
        )

    a = adapter()
    a.publish.side_effect = send
    async with f() as s1, f() as s2:
        svc1, _ = service(s1, a)
        svc2, _ = service(s2, adapter(ResultStatus.FAILED, FailureKind.RATE_LIMIT))
        task = asyncio.create_task(svc1.retry_channel(CONTENT, "telegram", publication_id="old"))
        await entered.wait()
        try:
            await svc2.retry_channel(CONTENT, "telegram", publication_id="new")
            before = await snapshot(f)
        finally:
            release.set()
            await task
    after = await snapshot(f)
    assert (after.state, after.epoch, after.failure_count, after.open_until) == (
        before.state,
        before.epoch,
        before.failure_count,
        before.open_until,
    )


async def test_cb11_concurrent_failure_accounting_serializable(db, monkeypatch):
    f, _ = db
    both_entered, release = asyncio.Event(), asyncio.Event()
    calls, settlements = [], []
    original_settle = CircuitBreakerRepository.settle

    async def observe(self, *args, **kw):
        accepted = await original_settle(self, *args, **kw)
        settlements.append(accepted)
        return accepted

    monkeypatch.setattr(CircuitBreakerRepository, "settle", observe)

    async def send(*args, **kw):
        calls.append(kw["publication_id"])
        if len(calls) == 2:
            both_entered.set()
        await release.wait()
        return ChannelResult("telegram", ResultStatus.FAILED, failure_kind=FailureKind.RATE_LIMIT)

    a = adapter()
    a.publish.side_effect = send
    async with f() as s1, f() as s2:
        svc1, _ = service(s1, a)
        svc2, _ = service(s2, a)
        tasks = [
            asyncio.create_task(svc.retry_channel(CONTENT, "telegram", publication_id=pub))
            for svc, pub in [(svc1, "one"), (svc2, "two")]
        ]
        await both_entered.wait()
        release.set()
        await asyncio.gather(*tasks)
    assert sorted(calls) == ["one", "two"]
    assert sorted(settlements) == [False, True]  # explicit serializable rejection
    assert (await snapshot(f)).failure_count == 1


@pytest.mark.parametrize(
    "status,failure,expected",
    [
        (ResultStatus.DONE, FailureKind.UNKNOWN, "closed"),
        (ResultStatus.FAILED, FailureKind.RATE_LIMIT, "open"),
    ],
)
async def test_cb12_half_open_settlement(db, status, failure, expected):
    f, _ = db
    await trip(f)
    async with f() as s:
        svc, _ = service(s, adapter(status, failure), clock=lambda: T0 + timedelta(seconds=30))
        await svc.retry_channel(CONTENT, "telegram", publication_id="probe")
    assert str((await snapshot(f)).state) == expected


async def test_cb13_submitted_terminal_not_health_success(db):
    f, _ = db
    await trip(f)
    async with f() as s:
        a = adapter(ResultStatus.SUBMITTED)
        svc, build = service(s, a, clock=lambda: T0 + timedelta(seconds=30))
        assert (
            await svc.retry_channel(CONTENT, "telegram", publication_id="p")
        ).status == "submitted"
        assert (
            await svc.retry_channel(CONTENT, "telegram", publication_id="p")
        ).status == "submitted"
        assert build.call_count == a.publish.call_count == 1
    assert str((await snapshot(f)).state) == "open"


async def test_cb14_restart_preserves_deadline_and_boundary(db):
    f, url = db
    await trip(f)
    engine = await create_engine_and_tables(url)
    fresh = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with fresh() as s:
            svc, build = service(s, adapter(), clock=lambda: T0 + timedelta(seconds=29))
            assert (
                await svc.retry_channel(CONTENT, "telegram", publication_id="p")
            ).status == "deferred"
            build.assert_not_called()
            svc, _ = service(s, adapter(), clock=lambda: T0 + timedelta(seconds=30))
            assert (
                await svc.retry_channel(CONTENT, "telegram", publication_id="p")
            ).status == "done"
    finally:
        await engine.dispose()


async def test_cb15_profiles_channels_and_targets_isolated(db):
    f, _ = db
    await trip(f)
    async with f() as s:
        svc, build = service(s, adapter())
        out = await svc.publish(
            CONTENT,
            [Selection("telegram", "a"), Selection("telegram", "b"), "vk_wall"],
            publication_id="p",
        )
        assert [o.status for o in out] == ["deferred", "deferred", "done"]
        assert build.call_count == 1
        svc2, b2 = service(s, adapter(), pid=2)
        assert (await svc2.retry_channel(CONTENT, "telegram", publication_id="p")).status == "done"
        assert b2.call_count == 1


async def test_cb16_browser_group_cooldown_after_blocked_target(db):
    f, _ = db
    await trip(f, channel="vk_wall")
    async with f() as s:
        repo = PublicationRepository(s, profile_id=1)
        p = await repo.create_scheduled(
            content_type="post",
            text="hi",
            title=None,
            media_paths=[],
            channels=[
                {"channel": "vk_wall", "target_id": "a"},
                {"channel": "vk_channel", "target_id": "b"},
                {"channel": "vk_wall", "target_id": "c"},
            ],
            scheduled_at=T0.replace(tzinfo=None),
        )
        post_id = p.id
    with patch(
        "crosspost.scheduler.executor.build_profile_adapter", AsyncMock(return_value=adapter())
    ) as build:
        moment = [T0]
        ex = ScheduledExecutor(
            f, pacing_seconds=0, browser_session_cooldown=300, clock=lambda: moment[0]
        )
        await ex.run_once(now=T0)
        assert [c.args[2] for c in build.call_args_list] == ["vk_channel"]
        # vk_channel consumed the shared VK bucket; expired wall breaker must not open an extra session.
        moment[0] += timedelta(seconds=31)
        await ex.run_once(now=moment[0])
        assert build.call_count == 1
        assert str((await snapshot(f, channel="vk_wall")).state) == "open"
    async with f() as s:
        assert str((await SchedulerRepository(s).get(post_id)).status) == "scheduled"


@pytest.mark.parametrize("mode", ["exception", "result"])
async def test_cb17_untrusted_platform_text_redacted(db, caplog, mode):
    f, _ = db
    async with f() as s:
        a = adapter()
        if mode == "exception":
            a.publish.side_effect = RuntimeError("FAKE_SECRET access_token=FAKE_SECRET")
        else:
            a.publish.return_value = ChannelResult(
                "telegram", ResultStatus.FAILED, error="FAKE_SECRET"
            )
        svc, _ = service(s, a)
        out = await svc.retry_channel(CONTENT, "telegram", publication_id="p")
        row = await svc._repo.get_status("p", "telegram")
        assert "FAKE_SECRET" not in repr(out) + str(row.error) + caplog.text


async def test_cb18_deployed_main_upgrade_preserves_data(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine, text

    from alembic import command
    from crosspost.db.migration_runner import migration_config
    from crosspost.db.models import Base

    path = tmp_path / "old.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        config = migration_config()
        config.attributes["connection"] = conn
        command.upgrade(config, "t201_unify")  # deployed main c650401
        conn.execute(text("INSERT INTO profiles(name) VALUES ('existing')"))
        conn.execute(
            text(
                "INSERT INTO publications(profile_id,publication_id,channel,target,status,external_id,attempt_count,updated_at) VALUES (1,'existing','telegram','','submitted','receipt',2,CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()
    upgraded = await create_engine_and_tables(f"sqlite+aiosqlite:///{path}")
    try:
        async with upgraded.connect() as conn:
            assert tuple(
                (
                    await conn.execute(
                        text(
                            "SELECT publication_id,status,external_id,attempt_count FROM publications"
                        )
                    )
                ).one()
            ) == ("existing", "submitted", "receipt", 2)
            assert (
                await conn.run_sync(
                    lambda c: compare_metadata(
                        MigrationContext.configure(c, opts={"compare_server_default": True}),
                        Base.metadata,
                    )
                )
                == []
            )
            assert (
                await conn.scalar(text("SELECT version_num FROM alembic_version")) == "t203_fencing"
            )
    finally:
        await upgraded.dispose()


async def test_cb05_http_429_retry_after_through_dispatch(db):
    f, _ = db
    async with f() as s:
        a = adapter()
        response = httpx.Response(
            429,
            headers={"Retry-After": "45"},
            request=httpx.Request("POST", "https://platform.invalid/?token=FAKE_SECRET"),
        )
        a.publish.side_effect = httpx.HTTPStatusError(
            "FAKE_SECRET", request=response.request, response=response
        )
        svc, build = service(s, a)
        out = await svc.publish(
            CONTENT, [Selection("telegram", "a"), Selection("telegram", "b")], publication_id="p"
        )
        assert [o.status for o in out] == ["failed", "deferred"]
        assert build.call_count == 1
        assert "FAKE_SECRET" not in repr(out)
    assert (await snapshot(f)).open_until == (T0 + timedelta(seconds=45)).replace(tzinfo=None)


@pytest.mark.parametrize("state", [ConnectionState.BANNED, ConnectionState.NEEDS_RELOGIN])
async def test_cb04_scheduled_connection_guard(db, state):
    f, _ = db
    async with f() as s:
        await ProfileRepository(s, vault=get_vault()).upsert_connection(1, "telegram", state)
        repo = PublicationRepository(s, profile_id=1)
        p = await repo.create_scheduled(
            content_type="post",
            text="hi",
            title=None,
            media_paths=[],
            channels=["telegram"],
            scheduled_at=T0.replace(tzinfo=None),
        )
        post_id = p.id
    with patch("crosspost.scheduler.executor.build_profile_adapter", AsyncMock()) as build:
        await ScheduledExecutor(f, pacing_seconds=0, clock=lambda: T0).run_once(now=T0)
        build.assert_not_called()
    async with f() as s:
        assert (
            await PublicationRepository(s, profile_id=1).get_status(
                scheduled_publication_id(post_id), "telegram"
            )
            is None
        )
        assert str((await SchedulerRepository(s).get(post_id)).status) == "scheduled"


@pytest.mark.parametrize("failure", [FailureKind.AUTH, FailureKind.BANNED])
@pytest.mark.parametrize("missing_connection", [False, True])
async def test_cb04_stale_credential_result_cannot_demote_reconnection(
    db, failure, missing_connection
):
    from sqlalchemy import delete

    from crosspost.db.models import Connection, CredentialKind

    f, _ = db
    entered, release = asyncio.Event(), asyncio.Event()
    async with f() as s:
        await ProfileRepository(s, vault=get_vault()).set_credential(
            1, "telegram", CredentialKind.API_TOKEN, "old"
        )
        if missing_connection:
            await s.execute(
                delete(Connection).where(
                    Connection.profile_id == 1, Connection.channel == "telegram"
                )
            )
            await s.commit()

    async def send(*args, **kw):
        entered.set()
        await release.wait()
        return ChannelResult(
            "telegram",
            ResultStatus.NEEDS_RELOGIN if failure == FailureKind.AUTH else ResultStatus.FAILED,
            failure_kind=failure,
        )

    a = adapter()
    a.publish.side_effect = send
    async with f() as s1, f() as s2:
        svc, _ = service(s1, a)
        task = asyncio.create_task(svc.retry_channel(CONTENT, "telegram", publication_id="p"))
        await entered.wait()
        try:
            await ProfileRepository(s2, vault=get_vault()).set_credential(
                1, "telegram", CredentialKind.API_TOKEN, "new"
            )
        finally:
            release.set()
            await task
        conn = await ProfileRepository(s2, vault=get_vault()).get_connection(1, "telegram")
        if missing_connection:
            assert conn is None  # No stale INSERT for the new credential either.
        else:
            assert str(conn.state) == "live"


async def test_cb01_open_article_defers_without_overwriting_channels(db):
    f, _ = db
    await trip(f, channel="telegraph")
    async with f() as s:
        svc, build = service(s, adapter())
        out = await svc.publish(
            CONTENT, ["telegram"], publication_id="p", article_text="article", article_title="Title"
        )
        assert len(out) == 1 and out[0].channel == "telegraph" and out[0].status == "deferred"
        assert await svc._repo.get_status("p", "telegram") is None
        build.assert_not_called()


async def test_cb05_typed_flood_during_factory_opens_breaker(db):
    from telethon.errors import FloodWaitError

    f, _ = db
    async with f() as s:
        svc, build = service(s, adapter())
        build.side_effect = FloodWaitError(request=None, capture=45)
        out = await svc.publish(
            CONTENT, [Selection("telegram", "a"), Selection("telegram", "b")], publication_id="p"
        )
        assert [o.status for o in out] == ["failed", "deferred"]
        assert build.call_count == 1
    assert (await snapshot(f)).open_until == (T0 + timedelta(seconds=45)).replace(tzinfo=None)


async def test_CB06_missing_connection_confirmed_ban_no_repeat(db):
    from sqlalchemy import delete

    from crosspost.db.models import Connection, CredentialKind

    f, _ = db
    async with f() as s:
        profiles = ProfileRepository(s, vault=get_vault())
        await profiles.set_credential(1, "telegram", CredentialKind.API_TOKEN, "legacy")
        await s.execute(
            delete(Connection).where(Connection.profile_id == 1, Connection.channel == "telegram")
        )
        await s.commit()
        assert await profiles.get_connection(1, "telegram") is None
        a = adapter(ResultStatus.FAILED, FailureKind.BANNED)
        svc, build = service(s, a)
        assert (await svc.retry_channel(CONTENT, "telegram", publication_id="p")).status == "failed"
        build.assert_awaited_once()
        a.publish.assert_awaited_once()
    # Separate session and a different publication: neither retry nor new work may send.
    async with f() as s:
        svc, build = service(s, adapter(), clock=lambda: T0 + timedelta(days=100))
        for publication_id in ["p", "new"]:
            out = await svc.retry_channel(CONTENT, "telegram", publication_id=publication_id)
            assert out.status == "banned"
        build.assert_not_called()
        conn = await ProfileRepository(s, vault=get_vault()).get_connection(1, "telegram")
        assert conn is not None and conn.state == ConnectionState.BANNED
        assert (await svc._repo.get_status("p", "telegram")).attempt_count == 1
        assert await svc._repo.get_status("new", "telegram") is None


@pytest.mark.parametrize("state", [ConnectionState.BANNED, ConnectionState.NEEDS_RELOGIN])
@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("path", ["immediate", "manual", "scheduled"])
@pytest.mark.parametrize("existing_publication", [False, True])
async def test_CB04_demotion_between_guard_and_attempt_no_bump(
    db, state, probe, path, existing_publication
):
    f, _ = db
    if probe:
        await trip(f)

    def clock():
        return T0 + timedelta(seconds=30)

    async with f() as s:
        repo = PublicationRepository(s, profile_id=1)
        if path == "scheduled":
            post = await repo.create_scheduled(
                content_type="post",
                text="hello",
                title=None,
                media_paths=[],
                channels=["telegram"],
                scheduled_at=T0.replace(tzinfo=None),
            )
            publication_id = scheduled_publication_id(post.id)
            post_id = post.id
        else:
            publication_id = "race"
        if existing_publication:
            await repo.bump_attempt(publication_id, "telegram")
            await repo.set_status(publication_id, "telegram", PublicationStatus.FAILED, error="old")
    permitted, demoted = asyncio.Event(), asyncio.Event()
    acquire = CircuitBreakerRepository.acquire

    async def pause_after_guard(self, *args, **kwargs):
        permit = await acquire(self, *args, **kwargs)
        assert permit is not None
        permitted.set()
        await demoted.wait()
        return permit

    a, build = adapter(), AsyncMock()
    build.return_value = a
    async with f() as worker_a, f() as worker_b:
        svc = PublishService(
            PublicationRepository(worker_a, profile_id=1),
            build,
            clock=clock,
            breaker_policy=POLICY,
        )
        with (
            patch.object(CircuitBreakerRepository, "acquire", pause_after_guard),
            patch("crosspost.scheduler.executor.build_profile_adapter", build),
        ):
            if path == "immediate":
                dispatch = svc.publish(CONTENT, ["telegram"], publication_id=publication_id)
            elif path == "manual":
                dispatch = svc.retry_channel(CONTENT, "telegram", publication_id=publication_id)
            else:
                dispatch = ScheduledExecutor(f, pacing_seconds=0, clock=clock).run_once(now=clock())
            task = asyncio.create_task(dispatch)
            await permitted.wait()
            try:
                await ProfileRepository(worker_b, vault=get_vault()).upsert_connection(
                    1, "telegram", state
                )
            finally:
                demoted.set()
                outcome = await task
            if path == "immediate":
                assert outcome[0].status == str(state)
            elif path == "manual":
                assert outcome.status == str(state)
        build.assert_not_called()
        a.publish.assert_not_called()
    async with f() as s:
        row = await PublicationRepository(s, profile_id=1).get_status(publication_id, "telegram")
        if existing_publication:
            assert (str(row.status), row.attempt_count, row.error) == ("failed", 1, "old")
        else:
            assert row is None
        if path == "scheduled":
            assert str((await SchedulerRepository(s).get(post_id)).status) == "scheduled"
    cb = await snapshot(f)
    assert str(cb.state) == ("open" if probe else "closed")
    assert cb.lease_until is None


@pytest.mark.parametrize("terminal", [PublicationStatus.DONE, PublicationStatus.SUBMITTED])
async def test_cb13_receipt_between_guard_and_attempt_is_preserved(db, terminal):
    f, _ = db
    await trip(f)
    permitted, completed = asyncio.Event(), asyncio.Event()
    acquire = CircuitBreakerRepository.acquire

    async def pause_after_guard(self, *args, **kwargs):
        permit = await acquire(self, *args, **kwargs)
        assert permit is not None and permit.probe
        permitted.set()
        await completed.wait()
        return permit

    async with f() as worker_a, f() as worker_b:
        svc, build = service(worker_a, adapter(), clock=lambda: T0 + timedelta(seconds=30))
        with patch.object(CircuitBreakerRepository, "acquire", pause_after_guard):
            task = asyncio.create_task(svc.retry_channel(CONTENT, "telegram", publication_id="p"))
            await permitted.wait()
            try:
                repo = PublicationRepository(worker_b, profile_id=1)
                await repo.bump_attempt("p", "telegram")
                await repo.set_status("p", "telegram", terminal, external_id="receipt")
            finally:
                completed.set()
                outcome = await task
        assert outcome.status == terminal.value
        build.assert_not_called()
        row = await svc._repo.get_status("p", "telegram")
        assert (row.status, row.attempt_count, row.external_id) == (terminal, 1, "receipt")
    cb = await snapshot(f)
    assert str(cb.state) == "open" and cb.lease_until is None
