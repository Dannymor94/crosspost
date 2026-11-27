"""Calendar move uses canonical CAS and cannot revive failed or moderated posts."""

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import Profile, Role, ScheduledPost, User
from crosspost.db.settings_repo import ProfileSettingsRepository
from crosspost.web import deps
from crosspost.web.app import create_app


@pytest.mark.asyncio
async def test_calendar_move_stale_claim_and_policy(tmp_path):
    engine = await create_engine_and_tables(f"sqlite+aiosqlite:///{tmp_path / 'calendar.db'}")
    try:
        initial = datetime(2099, 1, 15, 10)
        async with AsyncSession(engine) as session:
            session.add_all([Profile(id=1, name="One"), Profile(id=2, name="Two")])
            await session.flush()
            for post_id, pid, status, canonical in [
                (1, 1, "scheduled", True),
                (2, 1, "failed", True),
                (3, 1, "publishing", True),
                (4, 1, "pending_approval", True),
                (5, 1, "scheduled", False),
                (6, 2, "scheduled", True),
            ]:
                session.add(
                    ScheduledPost(
                        id=post_id,
                        profile_id=pid,
                        content_type="post",
                        text="unchanged",
                        channels=["telegram"],
                        scheduled_at=initial,
                        scheduled_at_utc=initial if canonical else None,
                        status=status,
                    )
                )
            await session.commit()
            await ProfileSettingsRepository(session, profile_id=1).set_schedule_timezone(
                "Europe/Berlin"
            )

        async def get_session():
            async with AsyncSession(engine) as session:
                yield session

        async def actor():
            return User(id=1, login="client", role=Role.CLIENT, profile_id=1, password_hash="x")

        app = create_app()
        app.dependency_overrides[deps.get_session] = get_session
        app.dependency_overrides[deps.get_current_user] = actor
        body = {
            "expected_at": initial.replace(tzinfo=UTC).isoformat(),
            "wall": "2099-01-16T12:00",
            "zone": "Europe/Berlin",
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            base = "/api/profiles/1/scheduled/"
            moved = await client.post(base + "1/move", json=body)
            assert moved.status_code == 200
            assert moved.json()["scheduled_at"] == "2099-01-16T11:00:00+00:00"
            for post_id in (1, 2, 3, 4, 5, 6):
                assert (await client.post(base + f"{post_id}/move", json=body)).status_code == 409
            assert (
                await client.post("/api/profiles/2/scheduled/6/move", json=body)
            ).status_code == 403
            current = {**body, "expected_at": moved.json()["scheduled_at"]}
            assert (
                await client.post(base + "1/move", json={**current, "zone": "Europe/Moscow"})
            ).status_code == 409
            assert (
                await client.post(base + "1/move", json={**current, "wall": "2027-10-31T02:30"})
            ).status_code == 422
            async with AsyncSession(engine) as session:
                await ProfileSettingsRepository(session, profile_id=1).set_client_moderation(True)
            assert (await client.post(base + "1/move", json=current)).status_code == 403
        async with AsyncSession(engine) as session:
            post = await session.get(ScheduledPost, 1)
            assert post is not None
            assert post.scheduled_at_utc == datetime(2099, 1, 16, 11)
            assert post.text == "unchanged" and post.channels == ["telegram"]
            failed = await session.get(ScheduledPost, 2)
            assert failed is not None and failed.status == "failed"
    finally:
        await engine.dispose()
