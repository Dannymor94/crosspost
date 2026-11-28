"""Роут управления клиентскими аккаунтами (только для админа). Часть 1.

Админ заводит client-аккаунт: login + пароль + привязка к профилю. Пароль
задаёт админ и передаёт клиенту вне системы (в ТГ). В ответе пароля/хэша нет.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError

from crosspost.auth.sessions import sign_recovery_token
from crosspost.db.log_repo import LogRepository
from crosspost.db.models import Role, User
from crosspost.web.auth_guard import begin_owner_step_up
from crosspost.web.deps import AdminUser, RepoDep, SessionDep, UserRepoDep
from crosspost.web.private_routes import PrivateRoute

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["users"], route_class=PrivateRoute)

_MIN_PASSWORD = 6


class ClientCreate(BaseModel):
    login: str
    password: str
    profile_id: int

    @field_validator("login")
    @classmethod
    def _login_ok(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Логин слишком короткий (мин. 3 символа)")
        return v

    @field_validator("password")
    @classmethod
    def _password_ok(cls, v: str) -> str:
        if len(v) < _MIN_PASSWORD:
            raise ValueError(f"Пароль слишком короткий (мин. {_MIN_PASSWORD} символов)")
        return v


class UserOut(BaseModel):
    id: int
    login: str
    role: Role
    profile_id: int | None

    model_config = {"from_attributes": True}


class RecoveryTokenOut(BaseModel):
    token: str
    expires_in_seconds: int


class RecoveryIssueBody(BaseModel):
    admin_password: str = Field(min_length=1, max_length=256)
    mfa_code: str = Field(default="", max_length=64)


@router.get("", response_model=list[UserOut])
async def list_clients(_: AdminUser, users: UserRepoDep) -> list[UserOut]:
    return [UserOut.model_validate(u) for u in await users.list_clients()]


@router.post("", response_model=UserOut, status_code=201)
async def create_client(
    body: ClientCreate,
    _: AdminUser,
    users: UserRepoDep,
    repo: RepoDep,
) -> UserOut:
    """Создать client-аккаунт с привязкой к существующему профилю."""
    if await repo.get_profile(body.profile_id) is None:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    if await users.get_by_login(body.login) is not None:
        raise HTTPException(status_code=409, detail=f"Логин '{body.login}' уже занят")
    try:
        user = await users.create_user(
            body.login, body.password, role=Role.CLIENT, profile_id=body.profile_id
        )
    except IntegrityError as exc:  # гонка по unique(login)
        raise HTTPException(status_code=409, detail="Логин уже занят") from exc
    return UserOut.model_validate(user)


@router.post("/{user_id}/recovery-token", response_model=RecoveryTokenOut)
async def issue_recovery_token(
    user_id: int,
    body: RecoveryIssueBody,
    admin: AdminUser,
    session: SessionDep,
    response: Response,
    request: Request,
) -> RecoveryTokenOut:
    """Только владелец выдаёт токен клиенту; пароль и сам токен не пишутся в журнал."""
    await begin_owner_step_up(request, session, admin, body.admin_password, body.mfa_code)
    user = await session.get(User, user_id)
    if user is None or user.role != Role.CLIENT or user.profile_id is None:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Клиент не найден")
    token = sign_recovery_token(user.id, generation=user.session_generation)
    await LogRepository(session).add(
        user.profile_id,
        channel="account",
        message=f"recovery_token_issued actor_id={admin.id} user_id={user.id}",
    )
    response.headers["Cache-Control"] = "no-store"
    return RecoveryTokenOut(token=token, expires_in_seconds=900)
