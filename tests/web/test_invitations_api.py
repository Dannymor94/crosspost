"""T-407: owner issuance, atomic redemption and secret-free persistence."""

import asyncio
import hashlib
import time

import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from crosspost.auth.totp import generate_secret, hotp
from crosspost.db.auth_attempt_repo import admit_attempt
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import AuthAttempt, ClientInvitation, Log, Profile, Role, User
from crosspost.db.owner_mfa import enroll_owner
from crosspost.db.user_repo import UserRepository
from crosspost.web import deps
from crosspost.web.app import create_app


@pytest_asyncio.fixture
async def registration(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SESSION_SECRET", "fixture-session-secret")
    monkeypatch.setenv("CLIENT_INVITATIONS_ENABLED", "true")
    engine = await create_engine_and_tables(f"sqlite+aiosqlite:///{tmp_path / 'registration.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(factory)
    async with factory() as session:
        p = Profile(name="existing-profile")
        session.add(p)
        await session.flush()
        users = UserRepository(session)
        await users.create_user("owner", "owner-password", role=Role.ADMIN)
        await users.create_user("existing", "existing-password", role=Role.CLIENT, profile_id=p.id)
    app = create_app()

    def client():
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    try:
        yield factory, client
    finally:
        await engine.dispose()


async def _login(c, login, password):
    response = await c.post("/api/auth/login", json={"login": login, "password": password})
    assert response.status_code == 200
    return response.json()


async def _invite(c, name):
    response = await c.post(
        "/api/users/invitations",
        json={"profile_name": name, "admin_password": "owner-password"},
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _body(token, login="new-client"):
    return {"token": token, "login": login, "new_password": "client-password-123"}


async def test_invite_is_owner_only_and_registers_one_isolated_client(registration, monkeypatch):
    factory, client = registration
    async with client() as owner, client() as anon, client() as existing:
        await _login(owner, "owner", "owner-password")
        old = await _login(existing, "existing", "existing-password")
        request = {"profile_name": "new-profile", "admin_password": "owner-password"}
        assert (await anon.post("/api/users/invitations", json=request)).status_code == 401
        assert (await existing.post("/api/users/invitations", json=request)).status_code == 403
        monkeypatch.setenv("CLIENT_INVITATIONS_ENABLED", "false")
        assert (await owner.post("/api/users/invitations", json=request)).status_code == 503
        monkeypatch.setenv("CLIENT_INVITATIONS_ENABLED", "true")
        wrong = {**request, "admin_password": "wrong"}
        assert (await owner.post("/api/users/invitations", json=wrong)).status_code == 403
        occupied = {**request, "profile_name": "existing-profile"}
        assert (await owner.post("/api/users/invitations", json=occupied)).status_code == 409
        invite = await _invite(owner, "new-profile")
        token = invite["token"]
        assert len(token) == 43
        assert invite["profile_id"] != old["profile_id"]
        listing = await owner.get("/api/users/invitations")
        assert token not in listing.text
        assert "token_hash" not in listing.text
        invalid = await anon.post("/api/auth/register", json={**_body(token), "role": "admin"})
        assert invalid.status_code == 422
        assert token not in invalid.text
        assert "client-password-123" not in invalid.text
        forged = {**_body(token), "profile_id": old["profile_id"]}
        assert (await anon.post("/api/auth/register", json=forged)).status_code == 422
        assert (await anon.post("/api/auth/register", json=_body(token))).status_code == 201
        replay = await anon.post("/api/auth/register", json=_body(token, "replay"))
        assert replay.status_code == 400
        assert (await anon.get("/api/auth/me")).status_code == 401
        new = await _login(anon, "new-client", "client-password-123")
        assert new["role"] == "client" and new["profile_id"] == invite["profile_id"]
        assert (await anon.get(f"/api/profiles/{old['profile_id']}")).status_code == 403
        assert (await existing.get(f"/api/profiles/{new['profile_id']}")).status_code == 403
    async with factory() as session:
        slot = await session.get(ClientInvitation, invite["id"])
        assert slot is not None and slot.consumed_at is not None
        assert slot.token_hash == hashlib.sha256(token.encode()).hexdigest()
        messages = list(await session.scalars(select(Log.message)))
        assert any("invite_issued" in message for message in messages)
        assert any("invite_redeemed" in message for message in messages)
        assert all(token not in m and "client-password-123" not in m for m in messages)


async def test_duplicate_login_rolls_back_and_concurrent_redeem_has_one_winner(registration):
    factory, client = registration
    async with client() as owner, client() as first, client() as second:
        await _login(owner, "owner", "owner-password")
        invite = await _invite(owner, "race-profile")
        token = invite["token"]
        duplicate = await first.post("/api/auth/register", json=_body(token, "existing"))
        assert duplicate.status_code == 409
        async with factory() as session:
            slot = await session.get(ClientInvitation, invite["id"])
            assert slot is not None and slot.consumed_at is None
        results = await asyncio.gather(
            first.post("/api/auth/register", json=_body(token, "race-first")),
            second.post("/api/auth/register", json=_body(token, "race-second")),
        )
        assert sorted(r.status_code for r in results) == [201, 400]
    async with factory() as session:
        query = select(User).where(User.profile_id == invite["profile_id"])
        clients = list(await session.scalars(query))
        assert len(clients) == 1
        assert clients[0].role == Role.CLIENT


async def test_revoke_rotate_expiry_and_existing_account_fence(registration):
    factory, client = registration
    async with client() as owner, client() as anon:
        await _login(owner, "owner", "owner-password")
        invite = await _invite(owner, "rotate-profile")
        path = f"/api/users/invitations/{invite['id']}"
        password = {"admin_password": "owner-password"}
        assert (await owner.post(path + "/revoke", json=password)).status_code == 200
        revoked = await anon.post("/api/auth/register", json=_body(invite["token"]))
        assert revoked.status_code == 400
        renewed = (await owner.post(path + "/renew", json=password)).json()
        assert renewed["token"] != invite["token"]
        async with factory() as session:
            await session.execute(
                update(ClientInvitation)
                .where(ClientInvitation.id == invite["id"])
                .values(expires_at=int(time.time()) - 1)
            )
            await session.commit()
        expired = await anon.post("/api/auth/register", json=_body(renewed["token"]))
        assert expired.status_code == 400
        current = (await owner.post(path + "/renew", json=password)).json()
        async with factory() as session:
            pid = invite["profile_id"]
            await UserRepository(session).create_user(
                "legacy-bound", "legacy-password", role=Role.CLIENT, profile_id=pid
            )
        occupied = await anon.post("/api/auth/register", json=_body(current["token"]))
        assert occupied.status_code == 400
        assert (await owner.post(path + "/renew", json=password)).status_code == 400


async def test_account_page_has_no_external_scripts_or_referrer(registration):
    _, client = registration
    async with client() as c:
        response = await c.get("/account")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert "/static/account.js" in response.text
        assert "https://" not in response.text


async def test_support_card_owner_only_and_scoped_audit(registration):
    factory, client = registration
    async with client() as owner, client() as anon, client() as existing:
        admin = await _login(owner, "owner", "owner-password")
        old = await _login(existing, "existing", "existing-password")
        invite = await _invite(owner, "support-profile")
        path = f"/api/users/support/{old['profile_id']}/inspect"
        assert (await anon.post(path, json={})).status_code == 401
        assert (await existing.post(path, json={})).status_code == 403
        response = await owner.post(path, json={})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        card = response.json()
        assert card["profile_id"] == old["profile_id"]
        assert card["accounts"] == [{"id": old["id"], "login": "existing"}]
        assert "password_hash" not in response.text
        assert "existing-password" not in response.text
        second = f"/api/users/support/{invite['profile_id']}/inspect"
        response = await owner.post(second, json={})
        assert response.status_code == 200
        assert response.json()["accounts"] == []
        assert (await owner.post("/api/users/support/999999/inspect", json={})).status_code == 404
    async with factory() as session:
        query = select(Log).where(Log.message.like("support_inspected%"))
        logs = list(await session.scalars(query))
        assert len(logs) == 2
        assert {log.profile_id for log in logs} == {old["profile_id"], invite["profile_id"]}
        assert all(f"actor_id={admin['id']}" in log.message for log in logs)


async def test_rate_limit_serialized_persistent_and_sliding(registration):
    factory, _ = registration

    async def attempt():
        async with factory() as session:
            return await admit_attempt(session, "login", "ip-key", "subject-key", now=10000)

    results = await asyncio.gather(*(attempt() for _ in range(8)))
    assert results.count(True) == 5
    async with factory() as session:
        assert not await admit_attempt(session, "login", "ip-key", "other", now=10899)
        assert await admit_attempt(session, "login", "ip-key", "other", now=10900)
        assert await admit_attempt(session, "login", "ip-key", "other", now=13600)
        rows = list(await session.scalars(select(AuthAttempt)))
        assert all(row.occurred_at > 10000 for row in rows)


async def test_rate_limit_http_spoof_and_secret_validation(registration, monkeypatch):
    factory, client = registration
    monkeypatch.setenv("CROSSPOST_AUTH_RATE_LIMIT", "true")
    async with client() as c:
        for i in range(5):
            r = await c.post(
                "/api/auth/login",
                json={"login": f"ghost-{i}", "password": "wrong"},
                headers={"X-Forwarded-For": f"192.0.2.{i + 1}"},
            )
            assert r.status_code == 401
        r = await c.post(
            "/api/auth/login",
            json={"login": "owner", "password": "owner-password"},
            headers={"X-Forwarded-For": "192.0.2.100"},
        )
        assert r.status_code == 429
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["retry-after"] == "3600"
        invalid = await c.post(
            "/api/auth/login", json={"login": {"secret": "do-not-echo"}, "password": "wrong"}
        )
        assert invalid.status_code == 422
        assert "do-not-echo" not in invalid.text
    async with factory() as session:
        rows = list(await session.scalars(select(AuthAttempt)))
        assert len(rows) == 5
        assert len({row.ip_key for row in rows}) == 1
        assert all(len(row.subject_key) == 64 for row in rows)


async def test_production_cookie_requires_https(registration, monkeypatch):
    _, client = registration
    monkeypatch.setenv("CROSSPOST_AUTH_MODE", "production")
    monkeypatch.setenv("CROSSPOST_AUTH_ORIGIN", "https://test")
    async with client() as c:
        r = await c.post(
            "/api/auth/login", json={"login": "existing", "password": "existing-password"}
        )
        assert r.status_code == 503
        assert "set-cookie" not in r.headers
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="https://test") as c:
        proof = (await c.get("/api/auth/csrf")).json()["token"]
        r = await c.post(
            "/api/auth/login",
            json={"login": "existing", "password": "existing-password"},
            headers={"X-CSRF-Token": proof, "Origin": "https://test"},
        )
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert (await c.get("/api/auth/me")).status_code == 200


async def test_csrf_requires_signed_browser_and_session_bound_proof(registration, monkeypatch):
    _, client = registration
    monkeypatch.setenv("CROSSPOST_CSRF_ENABLED", "true")
    async with client() as c, client() as other:
        body = {"login": "owner", "password": "owner-password"}
        assert (await c.post("/api/auth/login", json=body)).status_code == 403
        proof = await c.get("/api/auth/csrf")
        assert proof.status_code == 200
        assert proof.headers["cache-control"] == "no-store"
        header = {"X-CSRF-Token": proof.json()["token"], "Origin": "http://test"}
        assert (await other.post("/api/auth/login", json=body, headers=header)).status_code == 403
        cross = {**header, "Origin": "https://evil.example"}
        assert (await c.post("/api/auth/login", json=body, headers=cross)).status_code == 403
        assert (await c.post("/api/auth/login", json=body, headers=header)).status_code == 200
        assert (await c.post("/api/auth/logout", headers=header)).status_code == 403
        fresh = {"X-CSRF-Token": (await c.get("/api/auth/csrf")).json()["token"]}
        assert (await c.post("/api/users/invitations", json={})).status_code == 403
        r = await c.post(
            "/api/users/invitations",
            json={"profile_name": "csrf-profile", "admin_password": "owner-password"},
            headers=fresh,
        )
        assert r.status_code == 201
        assert (await c.delete("/api/profiles/999999")).status_code == 403
        assert (await c.post("/api/auth/logout", headers=fresh)).status_code == 200
        assert (await c.post("/api/auth/logout", headers=fresh)).status_code == 403


async def test_logout_revokes_all_devices_but_not_other_accounts(registration):
    factory, client = registration
    async with client() as first, client() as second, client() as owner, client() as stale:
        account = await _login(first, "existing", "existing-password")
        await _login(second, "existing", "existing-password")
        await _login(owner, "owner", "owner-password")
        old_cookie = first.cookies.get("session")
        assert old_cookie
        stale.cookies.set("session", old_cookie)
        token = await owner.post(
            f"/api/users/{account['id']}/recovery-token",
            json={"admin_password": "owner-password"},
        )
        assert token.status_code == 200
        assert (await first.post("/api/auth/logout")).status_code == 200
        assert (await first.get("/api/auth/me")).status_code == 401
        assert (await second.get("/api/auth/me")).status_code == 401
        assert (await owner.get("/api/auth/me")).status_code == 200
        recovery = await first.post(
            "/api/auth/recover",
            json={"token": token.json()["token"], "new_password": "unused-password-123"},
        )
        assert recovery.status_code == 400
        await _login(second, "existing", "existing-password")
        assert (await stale.post("/api/auth/logout")).status_code == 200
        assert (await second.get("/api/auth/me")).status_code == 200
    async with factory() as session:
        user = await session.get(User, account["id"])
        assert user is not None and user.session_generation == 1


async def test_owner_mfa_enrollment_and_concurrent_factor_replay(registration):
    factory, client = registration
    secret = generate_secret()
    timestamp = int(time.time()) - 30
    code = hotp(secret, timestamp // 30)
    async with client() as old, client() as first, client() as second:
        account = await _login(old, "owner", "owner-password")
        async with factory() as session:
            assert await enroll_owner(session, "owner", "wrong", secret, code) is None
            codes = await enroll_owner(
                session, "owner", "owner-password", secret, code, now=timestamp
            )
            assert codes is not None
        assert (await old.get("/api/auth/me")).status_code == 401
        body = {"login": "owner", "password": "owner-password"}
        assert (await first.post("/api/auth/login", json=body)).status_code == 401
        current = hotp(secret, int(time.time()) // 30)
        otp_body = {**body, "mfa_code": current}
        results = await asyncio.gather(
            first.post("/api/auth/login", json=otp_body),
            second.post("/api/auth/login", json=otp_body),
        )
        assert sorted(r.status_code for r in results) == [200, 401]
        backup_body = {**body, "mfa_code": codes[0]}
        results = await asyncio.gather(
            first.post("/api/auth/login", json=backup_body),
            second.post("/api/auth/login", json=backup_body),
        )
        assert sorted(r.status_code for r in results) == [200, 401]
        async with factory() as session:
            user = await session.get(User, account["id"])
            assert user is not None
            assert user.mfa_secret and secret not in user.mfa_secret
            assert user.mfa_backup_hashes and all(c not in user.mfa_backup_hashes for c in codes)
            assert user.session_generation == 1


async def test_unenrolled_owner_cannot_use_production_session(registration, monkeypatch):
    _, client = registration
    async with client() as c:
        await _login(c, "owner", "owner-password")
        monkeypatch.setenv("CROSSPOST_AUTH_MODE", "production")
        assert (await c.get("/api/auth/me")).status_code == 401
    monkeypatch.setenv("CROSSPOST_AUTH_ORIGIN", "https://test")
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="https://test") as c:
        proof = (await c.get("/api/auth/csrf")).json()["token"]
        body = {"login": "owner", "password": "owner-password"}
        r = await c.post("/api/auth/login", json=body, headers={"X-CSRF-Token": proof})
        assert r.status_code == 403
        assert "set-cookie" not in r.headers
