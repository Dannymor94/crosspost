"""Read-only publication history; existing profile RBAC is the authority."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from crosspost.db.history_repo import HistoryCursor, HistoryPage, HistoryRepository, SourceFilter
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
    response.headers["Cache-Control"] = "private, no-store"
    try:
        cursor = HistoryCursor.decode(before) if before else None
        return await HistoryRepository(session, profile_id).page(limit=limit, before=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Некорректный курсор") from exc


class HistorySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: SourceFilter = "all"
    query: str = Field(default="", max_length=120)
    limit: int = Field(default=20, ge=1, le=50)
    before: str | None = Field(default=None, max_length=512)


@router.post("/api/profiles/{profile_id}/history/search", response_model=HistoryPage)
async def search_history(
    profile_id: int,
    body: HistorySearch,
    session: SessionDep,
    response: Response,
) -> HistoryPage:
    """Read-only filtered history; query is not placed in access-log URLs."""
    if await session.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    response.headers["Cache-Control"] = "private, no-store"
    try:
        cursor = HistoryCursor.decode(body.before) if body.before else None
        return await HistoryRepository(session, profile_id).page(
            limit=body.limit, before=cursor, source_filter=body.source, query=body.query.strip()
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Некорректный курсор") from exc
