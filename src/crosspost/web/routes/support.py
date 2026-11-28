"""Owner-only, explicitly audited inspection of one profile. No impersonation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select, text

from crosspost.db.models import Connection, Log, Profile, Publication, Role, ScheduledPost, User
from crosspost.db.settings_repo import ProfileSettingsRepository
from crosspost.web.deps import AdminUser, SessionDep

router = APIRouter(prefix="/api/users/support", tags=["support"])


class AccountSummary(BaseModel):
    id: int
    login: str


class ConnectionSummary(BaseModel):
    channel: str
    state: str


class OutcomeSummary(BaseModel):
    channel: str
    status: str


class SupportCard(BaseModel):
    profile_id: int
    profile_name: str
    accounts: list[AccountSummary]
    connections: list[ConnectionSummary]
    moderation_enabled: bool
    scheduled_counts: dict[str, int]
    recent_outcomes: list[OutcomeSummary]


@router.post("/{profile_id}/inspect", response_model=SupportCard)
async def inspect_profile(
    profile_id: int, admin: AdminUser, session: SessionDep, response: Response
) -> SupportCard:
    """One explicit inspection, with its audit committed before returning the card."""
    response.headers["Cache-Control"] = "no-store"
    # Separate from the session used to authenticate the admin. Keep the profile
    # snapshot and audit in one serialized SQLite transaction.
    await session.execute(text("BEGIN IMMEDIATE"))
    profile = await session.get(Profile, profile_id)
    if profile is None:
        await session.rollback()
        raise HTTPException(404, "Профиль не найден")
    accounts = await session.execute(
        select(User.id, User.login)
        .where(User.profile_id == profile_id, User.role == Role.CLIENT)
        .order_by(User.id)
    )
    connections = await session.execute(
        select(Connection.channel, Connection.state)
        .where(Connection.profile_id == profile_id)
        .order_by(Connection.channel)
    )
    counts = await session.execute(
        select(ScheduledPost.status, func.count())
        .where(ScheduledPost.profile_id == profile_id)
        .group_by(ScheduledPost.status)
    )
    outcomes = await session.execute(
        select(Publication.channel, Publication.status)
        .where(Publication.profile_id == profile_id)
        .order_by(Publication.updated_at.desc(), Publication.id.desc())
        .limit(10)
    )
    moderation = await ProfileSettingsRepository(
        session, profile_id=profile_id
    ).get_client_moderation()
    card = SupportCard(
        profile_id=profile.id,
        profile_name=profile.name,
        accounts=[AccountSummary(id=uid, login=login) for uid, login in accounts],
        connections=[
            ConnectionSummary(channel=channel, state=str(state)) for channel, state in connections
        ],
        moderation_enabled=moderation,
        scheduled_counts={str(state): count for state, count in counts},
        recent_outcomes=[
            OutcomeSummary(channel=channel, status=str(state)) for channel, state in outcomes
        ],
    )
    session.add(
        Log(
            profile_id=profile_id,
            channel="account",
            level="INFO",
            message=f"support_inspected actor_id={admin.id} profile_id={profile_id}",
        )
    )
    await session.commit()
    return card
