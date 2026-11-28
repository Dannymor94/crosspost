"""Owner-issued registration capability; deliberately disabled until release approval."""

from __future__ import annotations

import os
import re
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from crosspost.db.invitation_repo import InvitationRepository
from crosspost.db.models import ClientInvitation
from crosspost.web.auth_guard import begin_owner_step_up, limit_auth
from crosspost.web.deps import AdminUser, SessionDep
from crosspost.web.private_routes import PrivateRoute

router = APIRouter(tags=["invitations"], route_class=PrivateRoute)
_PRIVATE = {"Cache-Control": "no-store"}


def _enabled() -> None:
    if os.environ.get("CLIENT_INVITATIONS_ENABLED", "").strip().lower() != "true":
        raise HTTPException(503, "Регистрация пока недоступна", headers=_PRIVATE)


def _invalid() -> HTTPException:
    return HTTPException(400, "Приглашение недействительно или устарело", headers=_PRIVATE)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class OwnerPassword(StrictBody):
    admin_password: str = Field(min_length=1, max_length=256)
    mfa_code: str = Field(default="", max_length=64)


class IssueBody(OwnerPassword):
    profile_name: str = Field(min_length=1, max_length=255)


class RedeemBody(StrictBody):
    token: str = Field(min_length=43, max_length=43)
    login: str = Field(min_length=3, max_length=64)
    new_password: str = Field(min_length=12, max_length=256)


class SlotOut(BaseModel):
    id: int
    profile_id: int
    profile_name: str
    state: str
    expires_at: int


class TokenOut(SlotOut):
    token: str


def _slot(invite: ClientInvitation) -> SlotOut:
    state = "used" if invite.consumed_at is not None else "active"
    if state == "active" and invite.expires_at <= int(time.time()):
        state = "revoked" if invite.expires_at == 0 else "expired"
    return SlotOut(
        id=invite.id,
        profile_id=invite.profile_id,
        profile_name=invite.profile_name,
        state=state,
        expires_at=invite.expires_at,
    )


@router.get("/api/users/invitations", response_model=list[SlotOut])
async def list_invitations(_: AdminUser, session: SessionDep) -> list[SlotOut]:
    _enabled()
    return [_slot(item) for item in await InvitationRepository(session).list_slots()]


@router.post("/api/users/invitations", response_model=TokenOut, status_code=201)
async def issue(
    body: IssueBody, admin: AdminUser, session: SessionDep, request: Request
) -> TokenOut:
    _enabled()
    await begin_owner_step_up(request, session, admin, body.admin_password, body.mfa_code)
    name = body.profile_name.strip()
    if not name:
        raise HTTPException(422, "Укажите название нового профиля", headers=_PRIVATE)
    try:
        invite, token = await InvitationRepository(session).create(name, admin.id)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Не удалось создать новый профиль", headers=_PRIVATE) from None
    return TokenOut(**_slot(invite).model_dump(), token=token)


@router.post("/api/users/invitations/{invite_id}/renew", response_model=TokenOut)
async def renew(
    invite_id: int, body: OwnerPassword, admin: AdminUser, session: SessionDep, request: Request
) -> TokenOut:
    _enabled()
    await begin_owner_step_up(request, session, admin, body.admin_password, body.mfa_code)
    result = await InvitationRepository(session).rotate(invite_id, admin.id)
    if result is None:
        raise _invalid()
    invite, token = result
    return TokenOut(**_slot(invite).model_dump(), token=token)


@router.post("/api/users/invitations/{invite_id}/revoke")
async def revoke(
    invite_id: int, body: OwnerPassword, admin: AdminUser, session: SessionDep, request: Request
) -> dict[str, bool]:
    _enabled()
    await begin_owner_step_up(request, session, admin, body.admin_password, body.mfa_code)
    if await InvitationRepository(session).rotate(invite_id, admin.id, revoke=True) is None:
        raise _invalid()
    return {"ok": True}


@router.post("/api/auth/register", status_code=201)
async def redeem(
    body: RedeemBody, session: SessionDep, response: Response, request: Request
) -> dict[str, bool]:
    _enabled()
    await limit_auth(request, session, "register", body.token)
    response.headers["Cache-Control"] = "no-store"
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", body.token):
        raise _invalid()
    login = body.login.strip()
    if not 3 <= len(login) <= 64:
        raise HTTPException(422, "Укажите логин от 3 до 64 символов", headers=_PRIVATE)
    try:
        ok = await InvitationRepository(session).redeem(body.token, login, body.new_password)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Логин недоступен", headers=_PRIVATE) from None
    if not ok:
        raise _invalid()
    return {"ok": True}
