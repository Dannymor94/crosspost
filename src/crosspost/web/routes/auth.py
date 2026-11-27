"""Роут авторизации: вход, выход, «кто я». Часть 1.

Вход по login+паролю → подписанная httponly-кука с user_id (пароль в куке НЕ лежит).
Вход, восстановление через владельца и ограничение попыток (T-406/T-409).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from crosspost.auth.passwords import hash_password, verify_password
from crosspost.auth.sessions import sign_session, verify_recovery_token
from crosspost.db.models import Role
from crosspost.web.auth_guard import limit_auth, production_mode, require_https
from crosspost.web.csrf import issue_proof
from crosspost.web.deps import SESSION_COOKIE, CurrentUser, SessionDep, UserRepoDep
from crosspost.web.private_routes import PrivateRoute

router = APIRouter(prefix="/api/auth", tags=["auth"], route_class=PrivateRoute)

_SESSION_MAX_AGE = 30 * 24 * 3600  # 30 дней — совпадает с sessions._MAX_AGE

# Фиктивный хэш: сверяем пароль ДАЖЕ когда логина нет, чтобы время ответа не
# выдавало существование аккаунта (защита от перебора логинов).
_DUMMY_HASH = hash_password("crosspost-dummy-constant")


class LoginBody(BaseModel):
    login: str = Field(max_length=1024)
    password: str = Field(max_length=4096)


class RecoverBody(BaseModel):
    token: str = Field(max_length=512)
    new_password: str = Field(min_length=12, max_length=256)


class MeOut(BaseModel):
    id: int
    login: str
    role: Role
    profile_id: int | None

    model_config = {"from_attributes": True}


@router.post("/login", response_model=MeOut)
async def login(
    body: LoginBody, response: Response, users: UserRepoDep, session: SessionDep, request: Request
) -> MeOut:
    secure = require_https(request)
    await limit_auth(request, session, "login", body.login.strip().casefold())
    user = await users.get_by_login(body.login.strip())
    # Всегда выполняем verify_password (по фиктивному хэшу, если юзера нет) —
    # постоянное время ответа, не раскрываем, какой логин существует.
    reference = user.password_hash if user is not None else _DUMMY_HASH
    ok = await asyncio.to_thread(verify_password, body.password, reference)
    if user is None or not ok:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    response.set_cookie(
        SESSION_COOKIE,
        sign_session(user.id, generation=user.session_generation),
        max_age=_SESSION_MAX_AGE,
        httponly=True,  # JS не читает куку → токен не украсть XSS-ом
        secure=secure,
        samesite="strict" if production_mode() else "lax",
        path="/",
    )
    return MeOut.model_validate(user)


@router.post("/recover")
async def recover(
    body: RecoverBody,
    response: Response,
    users: UserRepoDep,
    session: SessionDep,
    request: Request,
) -> dict[str, bool]:
    """Клиент задаёт новый пароль по одноразовому токену, выданному владельцем."""
    response.headers["Cache-Control"] = "no-store"
    await limit_auth(request, session, "recover", body.token)
    claims = verify_recovery_token(body.token)
    if claims is None:
        raise HTTPException(status_code=400, detail="Ссылка недействительна или устарела")
    user_id, generation = claims
    if not await users.recover_client_password(
        user_id, generation=generation, new_password=body.new_password
    ):
        raise HTTPException(status_code=400, detail="Ссылка недействительна или устарела")
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response, request: Request) -> dict[str, bool]:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=require_https(request),
        httponly=True,
        samesite="strict" if production_mode() else "lax",
    )
    return {"ok": True}


@router.get("/csrf")
async def csrf(request: Request, response: Response) -> dict[str, str | bool]:
    return issue_proof(request, response)


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser) -> MeOut:
    return MeOut.model_validate(user)
