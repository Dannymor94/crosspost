"""Initial API credential admission; never replace an existing connection."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, text

from crosspost.db.models import (
    Connection,
    ConnectionState,
    Credential,
    CredentialKind,
    Log,
    Profile,
    User,
)
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.vault import encrypt_blob


async def save_initial_connection(
    repo: ProfileRepository,
    *,
    profile_id: int,
    profile_created_at: datetime,
    actor_id: int,
    generation: int,
    channel: str,
    plaintext: str,
    expires_at: datetime | None,
) -> bool:
    session = repo.session
    await session.execute(text("BEGIN IMMEDIATE"))
    actor = await session.get(User, actor_id)
    profile = await session.get(Profile, profile_id)
    if (
        actor is None
        or actor.session_generation != generation
        or actor.profile_id != profile_id
        or actor.role != "client"
        or profile is None
        or profile.created_at != profile_created_at
    ):
        await session.rollback()
        return False
    occupied = await session.scalar(
        select(Credential.id).where(
            Credential.profile_id == profile_id, Credential.channel == channel
        )
    )
    if occupied is not None or await repo.get_connection(profile_id, channel) is not None:
        await session.rollback()
        return False
    session.add(
        Credential(
            profile_id=profile_id,
            channel=channel,
            kind=CredentialKind.API_TOKEN,
            blob=encrypt_blob(repo.vault, plaintext),
        )
    )
    session.add(
        Connection(
            profile_id=profile_id,
            channel=channel,
            state=ConnectionState.LIVE,
            expires_at=expires_at,
        )
    )
    session.add(
        Log(
            profile_id=profile_id,
            channel=channel,
            level="INFO",
            message=f"initial_connection_verified actor_id={actor_id}",
        )
    )
    await session.commit()
    return True
