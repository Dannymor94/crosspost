"""History read model: profile RBAC, ties, observed status and private fields."""

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import Profile, Publication, Role, ScheduledPost, User
from crosspost.web import deps
from crosspost.web.app import create_app


@pytest.mark.asyncio
async def test_history_scope_equal_timestamp_and_observed_outcomes(tmp_path):
    engine = await create_engine_and_tables(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    try:
        stamp = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
        async with AsyncSession(engine) as session:
            session.add_all([Profile(id=1, name="One"), Profile(id=2, name="Two")])
            await session.flush()
            session.add_all(
                [
                    ScheduledPost(
                        id=1,
                        profile_id=1,
                        content_type="post",
                        text="cancelled",
                        scheduled_at=stamp,
                        created_at=stamp,
                        status="cancelled",
                    ),
                    ScheduledPost(
                        id=2,
                        profile_id=1,
                        content_type="post",
                        text="planned",
                        scheduled_at=stamp,
                        created_at=stamp,
                        status="scheduled",
                    ),
                    ScheduledPost(
                        id=3,
                        profile_id=2,
                        content_type="post",
                        text="PRIVATE",
                        scheduled_at=stamp,
                        created_at=stamp,
                        status="scheduled",
                    ),
                    Publication(
                        profile_id=1,
                        publication_id="immediate-a",
                        channel="telegram",
                        target="",
                        status="submitted",
                        created_at=stamp,
                        error="PRIVATE_ERROR",
                        external_id="PRIVATE_ID",
                    ),
                    Publication(
                        profile_id=1,
                        publication_id="immediate-b",
                        channel="vk_wall",
                        target="1",
                        status="done",
                        created_at=stamp,
                    ),
                    Publication(
                        profile_id=1,
                        publication_id="immediate-b",
                        channel="telegram",
                        target="",
                        status="failed",
                        created_at=stamp,
                    ),
                    Publication(
                        profile_id=2,
                        publication_id="other",
                        channel="telegram",
                        target="",
                        status="done",
                        created_at=stamp,
                    ),
                ]
            )
            await session.commit()

        async def get_session():
            async with AsyncSession(engine) as session:
                yield session

        async def actor():
            return User(id=1, login="client", role=Role.CLIENT, profile_id=1, password_hash="x")

        app = create_app()
        app.dependency_overrides[deps.get_session] = get_session
        app.dependency_overrides[deps.get_current_user] = actor
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            url = "/api/profiles/1/history"
            entries = []
            cursor = None
            for _ in range(4):
                params = {"limit": "1"}
                if cursor:
                    params["before"] = cursor
                response = await client.get(url, params=params)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "private, no-store"
                assert "PRIVATE" not in response.text
                page = response.json()
                entries.extend(page["entries"])
                cursor = page["next_cursor"]
            assert cursor is None
            assert [(e["source"], e["key"]) for e in entries] == [
                ("scheduled", "2"),
                ("scheduled", "1"),
                ("immediate", "immediate-b"),
                ("immediate", "immediate-a"),
            ]
            assert [e["status"] for e in entries] == [
                "scheduled",
                "cancelled",
                "mixed_observed",
                "submitted_observed",
            ]
            assert entries[-1]["text"] is None
            assert all(e["coverage"] == "observed_only" for e in entries)
            assert (await client.get("/api/profiles/2/history")).status_code == 403
            assert (await client.get(url, params={"before": "not-a-cursor"})).status_code == 422

            search = url + "/search"
            first = await client.post(search, json={"query": "telegram", "limit": 1})
            assert first.status_code == 200
            assert first.json()["entries"][0]["status"] == "mixed_observed"
            assert len(first.json()["entries"][0]["outcomes"]) == 2
            cursor = first.json()["next_cursor"]
            assert cursor
            second = await client.post(search, json={"query": "telegram", "before": cursor})
            assert [e["key"] for e in second.json()["entries"]] == ["immediate-a"]
            assert (
                await client.post(search, json={"query": "vk_wall", "before": cursor})
            ).status_code == 422
            for query, source, expected in [
                ("planned", "scheduled", ["2"]),
                ("planned", "immediate", []),
                ("PRIVATE", "all", []),
                ("%", "all", []),
                ("telegram", "scheduled", []),
            ]:
                result = await client.post(search, json={"query": query, "source": source})
                assert result.status_code == 200
                assert [e["key"] for e in result.json()["entries"]] == expected
            assert (
                await client.post("/api/profiles/2/history/search", json={"query": "PRIVATE"})
            ).status_code == 403
    finally:
        await engine.dispose()
