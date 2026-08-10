"""Роут управления клиентскими аккаунтами (только для админа). Часть 1.

Админ заводит client-аккаунт: login + пароль + привязка к профилю. Пароль
задаёт админ и передаёт клиенту вне системы (в ТГ). В ответе пароля/хэша нет.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import IntegrityError

from crosspost.db.models import Role
from crosspost.web.deps import AdminUser, RepoDep, UserRepoDep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["users"])

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
