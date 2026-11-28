"""Atomic invitation issuance, rotation and redemption. No raw token persistence."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.auth.passwords import hash_password
from crosspost.db.models import ClientInvitation, Log, Profile, Role, User

TTL_SECONDS = 86400


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _eligible():
    # Match the original profile incarnation as well as ID; never adopt old profiles.
    original = exists().where(
        Profile.id == ClientInvitation.profile_id,
        Profile.name == ClientInvitation.profile_name,
        Profile.created_at == ClientInvitation.profile_created_at,
    )
    occupied = exists().where(User.profile_id == ClientInvitation.profile_id)
    return original & ~occupied & ClientInvitation.consumed_at.is_(None)


def _audit(session: AsyncSession, profile_id: int, message: str) -> None:
    session.add(Log(profile_id=profile_id, channel="account", level="INFO", message=message))


class InvitationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, name: str, actor_id: int) -> tuple[ClientInvitation, str]:
        token = secrets.token_urlsafe(32)
        profile = Profile(name=name)
        self.session.add(profile)
        await self.session.flush()
        invitation = ClientInvitation(
            profile_id=profile.id,
            profile_name=profile.name,
            profile_created_at=profile.created_at,
            token_hash=token_digest(token),
            expires_at=int(time.time()) + TTL_SECONDS,
        )
        self.session.add(invitation)
        await self.session.flush()
        _audit(self.session, profile.id, f"invite_issued actor_id={actor_id}")
        await self.session.commit()
        return invitation, token

    async def list_slots(self) -> list[ClientInvitation]:
        result = await self.session.scalars(
            select(ClientInvitation).order_by(ClientInvitation.id.desc())
        )
        return list(result)

    async def rotate(
        self, invite_id: int, actor_id: int, *, revoke: bool = False
    ) -> tuple[ClientInvitation, str] | None:
        token = secrets.token_urlsafe(32)
        result = await self.session.execute(
            update(ClientInvitation)
            .where(ClientInvitation.id == invite_id, _eligible())
            .values(
                token_hash=token_digest(token),
                expires_at=0 if revoke else int(time.time()) + TTL_SECONDS,
            )
            .returning(ClientInvitation)
        )
        invitation = result.scalar_one_or_none()
        if invitation is None:
            await self.session.rollback()
            return None
        action = "invite_revoked" if revoke else "invite_renewed"
        _audit(self.session, invitation.profile_id, f"{action} actor_id={actor_id}")
        await self.session.commit()
        return invitation, token

    async def redeem(self, token: str, login: str, password: str) -> bool:
        # Fast precheck avoids scrypt for invented tokens. End the read snapshot
        # before hashing; the conditional writer below is the authority.
        digest = token_digest(token)
        candidate = await self.session.scalar(
            select(ClientInvitation.id).where(
                ClientInvitation.token_hash == digest,
                ClientInvitation.expires_at > int(time.time()),
                _eligible(),
            )
        )
        await self.session.rollback()
        if candidate is None:
            return False
        hashed = await asyncio.to_thread(hash_password, password)
        now = int(time.time())
        result = await self.session.execute(
            update(ClientInvitation)
            .where(
                ClientInvitation.id == candidate,
                ClientInvitation.token_hash == digest,
                ClientInvitation.expires_at > now,
                _eligible(),
            )
            .values(consumed_at=now)
            .returning(ClientInvitation.profile_id)
        )
        profile_id = result.scalar_one_or_none()
        if profile_id is None:
            await self.session.rollback()
            return False
        user = User(login=login, password_hash=hashed, role=Role.CLIENT, profile_id=profile_id)
        self.session.add(user)
        await self.session.flush()
        _audit(self.session, profile_id, f"invite_redeemed user_id={user.id}")
        await self.session.commit()
        return True
