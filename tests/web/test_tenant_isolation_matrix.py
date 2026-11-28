"""T-410: keep every tenant-scoped API behind server-side authorization.

The route inventory is discovered from the real application, so a newly added
profile endpoint is automatically subject to the same A/B/anonymous matrix.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.auth.sessions import sign_session
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import Log, PublicationStatus, Role, ScheduledPostStatus
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.target_repo import ChannelTargetRepository
from crosspost.db.user_repo import UserRepository
from crosspost.db.vault import get_vault
from crosspost.web import deps
from crosspost.web.app import create_app

_APP = create_app()
_PROFILE_ROUTES = tuple(
    (method, route.path, route)
    for route in _APP.routes
    if isinstance(route, APIRoute) and "{profile_id}" in route.path
    for method in sorted(route.methods - {"HEAD", "OPTIONS"})
)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "test-tenant-isolation-key")
    monkeypatch.setenv("MEDIA_URL_SECRET", "test-media-secret")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("HEALTH_CHECK_ENABLED", "false")


@pytest_asyncio.fixture
async def tenants() -> AsyncGenerator[
    tuple[AsyncClient, dict[str, int], async_sessionmaker[AsyncSession]], None
]:
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(factory)
    async with factory() as session:
        profiles = ProfileRepository(session, vault=get_vault())
        a = await profiles.create_profile("tenant-A")
        b = await profiles.create_profile("tenant-B")
        users = UserRepository(session)
        alice = await users.create_user("alice", "alicepass", role=Role.CLIENT, profile_id=a.id)
        bob = await users.create_user("bob", "bobpass", role=Role.CLIENT, profile_id=b.id)
        admin = await users.create_user("owner", "adminpass", role=Role.ADMIN)
        ids = {"a": a.id, "b": b.id, "alice": alice.id, "bob": bob.id, "admin": admin.id}

    try:
        async with AsyncClient(transport=ASGITransport(app=_APP), base_url="http://test") as c:
            yield c, ids, factory
    finally:
        await engine.dispose()


def _callbacks(route: APIRoute) -> set[object]:
    """Include nested dependencies: require_admin -> get_current_user, etc."""
    callbacks: set[object] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        callbacks.add(dep.call)
        stack.extend(dep.dependencies)
    return callbacks


def _path(template: str, profile_id: int) -> str:
    path = template.replace("{profile_id}", str(profile_id))
    path = re.sub(r"\{target_value:path\}", "-100123456", path)
    for name, value in {
        "channel": "vk_wall",
        "action": "phone",
        "target_id": "999999",
        "scheduled_id": "999999",
        "publication_id": "missing-publication",
        "index": "0",
    }.items():
        path = path.replace("{" + name + "}", value)
    assert "{" not in path, f"Unmapped route parameter: {template}"
    return path


async def _request(c: AsyncClient, method: str, path: str, user_id: int | None):
    headers = _headers(user_id)
    # Empty payloads cannot create valid posts or trigger real platform calls;
    # authorization must happen before handler/body validation for foreign tenants.
    kwargs = {"json": {}} if method in {"POST", "PATCH", "PUT"} else {}
    return await c.request(method, path, headers=headers, **kwargs)


def _headers(user_id: int | None) -> dict[str, str]:
    return {"Cookie": f"session={sign_session(user_id)}"} if user_id is not None else {}


def test_all_profile_routes_have_an_authorization_dependency():
    """Mutation-killing guard: newly unprotected routes fail even without requests."""
    from crosspost.web.deps import require_admin, require_profile_access

    assert len(_PROFILE_ROUTES) >= 45  # prevent accidental router omission
    unprotected = [
        f"{method} {path}"
        for method, path, route in _PROFILE_ROUTES
        if not (_callbacks(route) & {require_profile_access, require_admin})
    ]
    assert not unprotected, f"Profile endpoints without server-side authorization: {unprotected}"


async def test_all_profile_routes_reject_anonymous_and_foreign_clients(tenants):
    c, ids, _factory = tenants
    for method, template, _route in _PROFILE_ROUTES:
        for profile_id, outsider in ((ids["a"], ids["bob"]), (ids["b"], ids["alice"])):
            path = _path(template, profile_id)
            anonymous = await _request(c, method, path, None)
            assert anonymous.status_code == 401, (
                f"Anonymous {method} {path} returned {anonymous.status_code}"
            )
            foreign = await _request(c, method, path, outsider)
            assert foreign.status_code == 403, (
                f"Foreign tenant {method} {path} returned {foreign.status_code}"
            )


async def test_profile_listing_and_details_are_separate_for_both_clients(tenants):
    c, ids, _factory = tenants
    for own, foreign, user in (
        (ids["a"], ids["b"], ids["alice"]),
        (ids["b"], ids["a"], ids["bob"]),
    ):
        headers = _headers(user)
        listing = await c.get("/api/profiles", headers=headers)
        assert listing.status_code == 200
        assert [p["id"] for p in listing.json()] == [own]
        own_profile = await c.get(f"/api/profiles/{own}", headers=headers)
        assert own_profile.status_code == 200
        assert own_profile.json()["id"] == own
        assert (await c.get(f"/api/profiles/{foreign}", headers=headers)).status_code == 403

    admin_headers = _headers(ids["admin"])
    listing = await c.get("/api/profiles", headers=admin_headers)
    assert listing.status_code == 200
    assert {p["id"] for p in listing.json()} == {ids["a"], ids["b"]}
    for profile_id in (ids["a"], ids["b"]):
        assert (
            await c.get(f"/api/profiles/{profile_id}", headers=admin_headers)
        ).status_code == 200


async def test_foreign_nested_target_id_cannot_be_read_modified_or_deleted(tenants):
    """Even a *correct own profile_id* cannot authorize an ID owned by B."""
    c, ids, factory = tenants
    async with factory() as session:
        targets = ChannelTargetRepository(session, vault=get_vault(), profile_id=ids["b"])
        foreign = await targets.add_target("vk_wall", "private-B", "privateb")
        target_id = foreign.id

    own_path = f"/api/profiles/{ids['a']}/channels/vk_wall/targets/{target_id}"
    alice = _headers(ids["alice"])
    assert (await c.patch(own_path, json={"name": "stolen"}, headers=alice)).status_code == 404
    assert (await c.delete(own_path, headers=alice)).status_code == 404
    assert (
        await c.get(f"/api/profiles/{ids['a']}/channels/vk_wall/targets", headers=alice)
    ).json() == []

    bob = _headers(ids["bob"])
    own_b = await c.get(f"/api/profiles/{ids['b']}/channels/vk_wall/targets", headers=bob)
    assert own_b.status_code == 200
    assert [(item["id"], item["name"]) for item in own_b.json()] == [(target_id, "private-B")]


async def test_nested_scheduled_media_report_and_publications_stay_in_owner_profile(tenants):
    """A may know B's real IDs; the URL's A profile must still filter every DB read."""
    c, ids, factory = tenants
    media_root = Path(os.environ["MEDIA_DIR"])
    media_root.mkdir(parents=True)
    private_file = media_root / "private-b.png"
    private_file.write_bytes(b"B private media")

    async with factory() as session:
        posts_b = PublicationRepository(session, profile_id=ids["b"])
        post = await posts_b.create_scheduled(
            content_type="post",
            text="B confidential post",
            title=None,
            media_paths=[str(private_file)],
            channels=["vk_wall"],
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
        )
        scheduled_id = post.id
        from crosspost.scheduler.executor import scheduled_publication_id

        publication_id = scheduled_publication_id(scheduled_id)
        await posts_b.set_status(publication_id, "vk_wall", PublicationStatus.FAILED)
        post.status = ScheduledPostStatus.FAILED
        post.published_at = datetime.now(UTC)
        session.add(
            Log(
                profile_id=ids["b"],
                publication_id=publication_id,
                channel="vk_wall",
                level="ERROR",
                message="B confidential error",
                screenshot_path="/private-b-screenshot.png",
            )
        )
        await session.commit()

    alice = _headers(ids["alice"])
    bob = _headers(ids["bob"])
    a_base = f"/api/profiles/{ids['a']}"
    b_base = f"/api/profiles/{ids['b']}"

    # IDs from a different tenant cannot be looked up inside A's selected profile.
    for suffix in (f"/scheduled/{scheduled_id}", f"/scheduled/{scheduled_id}/media/0"):
        assert (await c.get(a_base + suffix, headers=alice)).status_code == 404
    assert (
        await c.patch(f"{a_base}/scheduled/{scheduled_id}", json={"text": "stolen"}, headers=alice)
    ).status_code == 404
    assert (await c.delete(f"{a_base}/scheduled/{scheduled_id}", headers=alice)).status_code == 404
    assert (
        await c.post(f"{a_base}/scheduled/{scheduled_id}/publish-now", headers=alice)
    ).status_code == 404

    assert (await c.get(f"{a_base}/scheduled/report", headers=alice)).json() == []
    assert (
        await c.get(f"{a_base}/scheduled/{scheduled_id}/screenshots", headers=alice)
    ).json() == {}
    assert (await c.get(f"{a_base}/publish/{publication_id}", headers=alice)).json() == []

    assert (await c.get(f"{b_base}/scheduled/{scheduled_id}", headers=bob)).json()[
        "text"
    ] == "B confidential post"
    assert (
        await c.get(f"{b_base}/scheduled/{scheduled_id}/media/0", headers=bob)
    ).content == b"B private media"
    report = (await c.get(f"{b_base}/scheduled/report", headers=bob)).json()
    assert len(report) == 1 and report[0]["id"] == scheduled_id
    assert (await c.get(f"{b_base}/scheduled/{scheduled_id}/screenshots", headers=bob)).json() == {
        "vk_wall": "/private-b-screenshot.png"
    }
    assert len((await c.get(f"{b_base}/publish/{publication_id}", headers=bob)).json()) == 1


async def test_new_history_move_and_drafts_remain_tenant_scoped(tenants):
    from crosspost.db.settings_repo import ProfileSettingsRepository

    c, ids, factory = tenants
    initial = datetime(2099, 1, 15, 10)
    async with factory() as session:
        posts = PublicationRepository(session, profile_id=ids["b"])
        post = await posts.create_scheduled(
            content_type="post",
            text="B private history",
            title=None,
            media_paths=[],
            channels=["telegram"],
            scheduled_at=initial,
            scheduled_at_utc=initial,
        )
        post_id = post.id
        for pid in (ids["a"], ids["b"]):
            await ProfileSettingsRepository(session, profile_id=pid).set_schedule_timezone(
                "Europe/Berlin"
            )

    alice, bob = _headers(ids["alice"]), _headers(ids["bob"])
    a_base, b_base = (f"/api/profiles/{ids[k]}" for k in ("a", "b"))
    body = {
        "expected_at": "2099-01-15T10:00:00Z",
        "wall": "2099-01-16T12:00",
        "zone": "Europe/Berlin",
    }
    assert (
        await c.post(f"{a_base}/scheduled/{post_id}/move", json=body, headers=alice)
    ).status_code == 409
    assert (await c.get(a_base + "/history", headers=alice)).json()["entries"] == []
    assert (
        await c.post(a_base + "/history/search", json={"query": "B private"}, headers=alice)
    ).json()["entries"] == []
    history = await c.get(b_base + "/history", headers=bob)
    assert history.status_code == 200
    assert history.json()["entries"][0]["text"] == "B private history"
    a_draft = (await c.get(a_base + "/editor-draft", headers=alice)).json()
    b_draft = (await c.get(b_base + "/editor-draft", headers=bob)).json()
    assert a_draft["scope"] != b_draft["scope"]
    denied = await c.put(
        a_base + "/editor-draft",
        headers=alice,
        json={"scope": b_draft["scope"], "revision": None, "data": {"text": "stolen"}},
    )
    assert denied.status_code == 409
    assert (await c.get(b_base + "/editor-draft", headers=bob)).json() == b_draft
    original = await c.get(f"{b_base}/scheduled/{post_id}", headers=bob)
    assert original.json()["scheduled_at"] == "2099-01-15T10:00:00+00:00"


def test_global_admin_endpoints_have_admin_guard():
    """The unscoped admin surface must never expose every tenant to clients."""
    from crosspost.web.deps import require_admin

    protected_prefixes = ("/api/admin/", "/api/users", "/api/settings/")
    routes = [
        route
        for route in _APP.routes
        if isinstance(route, APIRoute) and route.path.startswith(protected_prefixes)
    ]
    assert routes
    assert all(require_admin in _callbacks(route) for route in routes)
