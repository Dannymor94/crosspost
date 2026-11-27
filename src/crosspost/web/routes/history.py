"""Read-only publication history; existing profile RBAC is the authority."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from crosspost.db.history_repo import HistoryCursor, HistoryPage, HistoryRepository
from crosspost.db.models import Profile
from crosspost.web.deps import SessionDep, require_profile_access

router = APIRouter(tags=["history"], dependencies=[Depends(require_profile_access)])


@router.get("/api/profiles/{profile_id}/history", response_model=HistoryPage)
async def history(
    profile_id: int,
    session: SessionDep,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    before: Annotated[str | None, Query(max_length=512)] = None,
) -> HistoryPage:
    if await session.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    try:
        cursor = HistoryCursor.decode(before) if before else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Некорректный курсор") from exc
    response.headers["Cache-Control"] = "private, no-store"
    return await HistoryRepository(session, profile_id).page(limit=limit, before=cursor)
