"""Authenticated private draft read/write; CAS prevents cross-tab overwrite."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.sqlite import insert

from crosspost.db.models import EditorDraft, Profile, User
from crosspost.web.deps import CurrentUser, SessionDep, require_profile_access

router = APIRouter(tags=["drafts"], dependencies=[Depends(require_profile_access)])


class DraftData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: Literal["post", "article"] = "post"
    text: str = Field(default="", max_length=200_000)
    article_title: str = Field(default="", max_length=255)
    article_text: str = Field(default="", max_length=200_000)
    link_phrase: str = Field(default="", max_length=128)
    targets: list[str] = Field(default_factory=list, max_length=200)
    when: Literal["now", "later"] = "now"
    scheduled_at: str = Field(default="", max_length=64)
    media_required: bool = False


class DraftOut(BaseModel):
    scope: str
    revision: str | None
    data: DraftData | None


class DraftWrite(BaseModel):
    scope: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str | None = Field(pattern=r"^[0-9a-f]{32}$")
    data: DraftData | None


def _scope(user: User, profile: Profile) -> str:
    # Password rotation and identity replacement invalidate the previous scope.
    payload = [
        "editor-draft-v1",
        user.id,
        user.created_at.isoformat(),
        user.password_hash,
        str(user.role),
        user.profile_id,
        profile.id,
        profile.created_at.isoformat(),
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


async def _identity(session: SessionDep, user: User, profile_id: int) -> str:
    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    return _scope(user, profile)


@router.get("/api/profiles/{profile_id}/editor-draft", response_model=DraftOut)
async def get_draft(
    profile_id: int,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
    expected_actor: int | None = Header(None, alias="X-Draft-Actor"),
) -> DraftOut:
    response.headers["Cache-Control"] = "no-store"
    if expected_actor is not None and expected_actor != user.id:
        raise HTTPException(status_code=409, detail="Аккаунт изменился")
    scope = await _identity(session, user, profile_id)
    row = await session.scalar(
        select(EditorDraft).where(
            EditorDraft.scope == scope,
            EditorDraft.user_id == user.id,
            EditorDraft.profile_id == profile_id,
        )
    )
    if row is None:
        return DraftOut(scope=scope, revision=None, data=None)
    data = DraftData.model_validate(row.data) if row.data else None
    return DraftOut(scope=scope, revision=row.revision, data=data)


@router.put("/api/profiles/{profile_id}/editor-draft", response_model=DraftOut)
async def put_draft(
    profile_id: int, body: DraftWrite, session: SessionDep, user: CurrentUser, response: Response
) -> DraftOut:
    response.headers["Cache-Control"] = "no-store"
    scope = await _identity(session, user, profile_id)
    if not hmac.compare_digest(scope, body.scope):
        raise HTTPException(status_code=409, detail="Аккаунт или профиль изменился")
    if body.data is not None and any(len(key) > 512 for key in body.data.targets):
        raise HTTPException(status_code=422, detail="Некорректный список целей")
    revision = secrets.token_hex(16)
    data = body.data.model_dump() if body.data is not None else {}
    # End the read transaction before competing SQLite writes.
    actor_id = user.id
    await session.rollback()
    if body.revision is None:
        create_stmt = (
            insert(EditorDraft)
            .values(
                scope=scope, user_id=actor_id, profile_id=profile_id, revision=revision, data=data
            )
            .on_conflict_do_nothing(index_elements=["scope"])
            .returning(EditorDraft.revision)
        )
        result = await session.execute(create_stmt)
    else:
        update_stmt = (
            sql_update(EditorDraft)
            .where(
                EditorDraft.scope == scope,
                EditorDraft.user_id == actor_id,
                EditorDraft.profile_id == profile_id,
                EditorDraft.revision == body.revision,
            )
            .values(revision=revision, data=data)
            .returning(EditorDraft.revision)
        )
        result = await session.execute(update_stmt)
    if result.scalar_one_or_none() is None:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Черновик изменён в другой вкладке")
    await session.commit()
    return DraftOut(scope=scope, revision=revision, data=body.data)
