"""Strict calendar move: no failed rewind, no dispatch, no legacy conversion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text, update

from crosspost.db.models import Profile, ScheduledPost
from crosspost.db.settings_repo import ProfileSettingsRepository
from crosspost.scheduler.schedule_time import parse_instant, wall_candidates
from crosspost.web.deps import CurrentUser, SessionDep, require_profile_access
from crosspost.web.moderation import require_unmoderated_action

router = APIRouter(tags=["calendar"], dependencies=[Depends(require_profile_access)])


class MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_at: str = Field(max_length=64)
    wall: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
    zone: str = Field(min_length=1, max_length=128)


class MoveResult(BaseModel):
    id: int
    scheduled_at: str
    status: str = "scheduled"


@router.post("/api/profiles/{profile_id}/scheduled/{scheduled_id}/move", response_model=MoveResult)
async def move(
    profile_id: int,
    scheduled_id: int,
    body: MoveBody,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
) -> MoveResult:
    # Serialize with executor claims and changes to moderation/timezone.
    await session.execute(text("BEGIN IMMEDIATE"))
    if await session.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    await require_unmoderated_action(user, session, profile_id)
    settings = ProfileSettingsRepository(session, profile_id=profile_id)
    configured = await settings.get_schedule_timezone()
    if configured != body.zone:
        raise HTTPException(status_code=409, detail="Часовой пояс изменился или не настроен")
    try:
        expected = parse_instant(body.expected_at)
        candidates = wall_candidates(datetime.fromisoformat(body.wall), ZoneInfo(body.zone))
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(status_code=422, detail="Некорректное время") from exc
    if len(candidates) != 1:
        raise HTTPException(status_code=422, detail="Выберите время вне перевода часов")
    instant = candidates[0]
    if instant <= datetime.now(UTC) + timedelta(seconds=5):
        raise HTTPException(status_code=422, detail="Выберите будущее время")
    if instant == expected:
        raise HTTPException(status_code=422, detail="Выберите другое время")
    result = await session.execute(
        update(ScheduledPost)
        .where(
            ScheduledPost.id == scheduled_id,
            ScheduledPost.profile_id == profile_id,
            ScheduledPost.status == "scheduled",
            ScheduledPost.scheduled_at_utc == expected.replace(tzinfo=None),
        )
        .values(
            scheduled_at=instant.astimezone().replace(tzinfo=None),
            scheduled_at_utc=instant.replace(tzinfo=None),
        )
        .returning(ScheduledPost.id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=409, detail="Пост или его время изменились. Обновите календарь."
        )
    await session.commit()
    response.headers["Cache-Control"] = "private, no-store"
    return MoveResult(id=scheduled_id, scheduled_at=instant.isoformat())
