"""Tests: client role cannot reach admin-only endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-secret")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")


@pytest.fixture()
def client(tmp_path):
    from crosspost.web.app import create_app

    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def seeded_app(tmp_path, monkeypatch):
    """App with an admin + a client user attached to profile 1."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from crosspost.auth.passwords import hash_password
    from crosspost.db.engine import create_engine_and_tables
    from crosspost.db.models import Profile, Role, User
    from crosspost.web.app import create_app

    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    monkeypatch.setenv("DB_URL", db_url)
    monkeypatch.setenv("VAULT_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    async def _seed():
        engine = await create_engine_and_tables(db_url)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            profile = Profile(name="Test Profile")
            session.add(profile)
            await session.flush()
            session.add(
                User(
                    login="admin",
                    password_hash=hash_password("adminpass"),
                    role=Role.ADMIN,
                    profile_id=None,
                )
            )
            session.add(
                User(
                    login="client1",
                    password_hash=hash_password("clientpass"),
                    role=Role.CLIENT,
                    profile_id=profile.id,
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_seed())

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_client_cannot_list_users(seeded_app):
    """Client must get 403/404 on admin-only /api/users."""
    c = seeded_app
    c.post("/api/auth/login", json={"login": "client1", "password": "clientpass"})
    r = c.get("/api/users")
    assert r.status_code in (403, 404)


def test_client_cannot_create_profile(seeded_app):
    """Client must get 403 when trying to create a profile (admin-only)."""
    c = seeded_app
    c.post("/api/auth/login", json={"login": "client1", "password": "clientpass"})
    r = c.post("/api/profiles", json={"name": "Hacked Profile"})
    assert r.status_code in (403, 404)


def test_client_cannot_read_other_profile(seeded_app):
    """Client gets 403 when accessing a profile they are not attached to."""
    c = seeded_app
    c.post("/api/auth/login", json={"login": "client1", "password": "clientpass"})
    r = c.get("/api/profiles/9999")
    assert r.status_code in (403, 404)


def test_client_cannot_access_other_profile_channels(seeded_app):
    """Client gets 403 when accessing a different profile's channels."""
    c = seeded_app
    c.post("/api/auth/login", json={"login": "client1", "password": "clientpass"})
    # profile 9999 doesn't belong to this client
    r = c.get("/api/profiles/9999/channels")
    assert r.status_code in (403, 404)


def test_client_cannot_approve_posts(seeded_app):
    """Approve endpoint is admin-only."""
    c = seeded_app
    c.post("/api/auth/login", json={"login": "client1", "password": "clientpass"})
    r = c.post("/api/profiles/1/scheduled/1/approve")
    assert r.status_code in (403, 404)


def test_client_cannot_set_moderation(seeded_app):
    """Moderation setting endpoint is admin-only."""
    c = seeded_app
    c.post("/api/auth/login", json={"login": "client1", "password": "clientpass"})
    r = c.put("/api/profiles/1/settings/moderation", json={"enabled": True})
    assert r.status_code in (403, 404)


def test_admin_can_set_and_get_moderation(seeded_app):
    """Admin can configure client moderation for a profile."""
    c = seeded_app
    c.post("/api/auth/login", json={"login": "admin", "password": "adminpass"})
    r = c.put("/api/profiles/1/settings/moderation", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    r2 = c.get("/api/profiles/1/settings/moderation")
    assert r2.status_code == 200
    assert r2.json()["enabled"] is True


def test_reject_body_optional(seeded_app):
    """Reject endpoint accepts empty body (reason defaults to '')."""
    c = seeded_app
    c.post("/api/auth/login", json={"login": "admin", "password": "adminpass"})
    # Post doesn't exist → 404/409, but no 422 (body parsing should succeed)
    r = c.post("/api/profiles/1/scheduled/9999/reject", json={})
    assert r.status_code in (404, 409)


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("POST", "/publish", {"data": {"channels": '["telegram"]', "text": "test"}}),
        ("POST", "/publish/example/retry/telegram", {}),
        ("POST", "/scheduled/1/publish-now", {}),
        ("PATCH", "/scheduled/1", {"json": {"text": "changed"}}),
        ("POST", "/import/commit", {"data": {"channels": '["telegram"]'}}),
    ],
)
def test_moderated_client_cannot_bypass_approval(seeded_app, method, path, kwargs):
    c = seeded_app
    login = c.post("/api/auth/login", json={"login": "admin", "password": "adminpass"})
    assert login.status_code == 200
    assert c.put("/api/profiles/1/settings/moderation", json={"enabled": True}).status_code == 200
    login = c.post("/api/auth/login", json={"login": "client1", "password": "clientpass"})
    assert login.status_code == 200
    response = c.request(method, "/api/profiles/1" + path, **kwargs)
    assert response.status_code == 403
    assert "модерац" in response.json()["detail"].lower()


def test_unmoderated_client_keeps_direct_publish_validation(seeded_app):
    c = seeded_app
    login = c.post("/api/auth/login", json={"login": "client1", "password": "clientpass"})
    assert login.status_code == 200
    response = c.post("/api/profiles/1/publish", data={"channels": "[]", "text": "test"})
    assert response.status_code == 422



def test_client_can_read_own_moderation_but_not_foreign(seeded_app):
    c = seeded_app
    login = c.post("/api/auth/login", json={"login": "client1", "password": "clientpass"})
    assert login.status_code == 200
    own = c.get("/api/profiles/1/settings/moderation")
    assert own.status_code == 200
    assert own.json() == {"enabled": False}
    assert c.get("/api/profiles/9999/settings/moderation").status_code == 403
