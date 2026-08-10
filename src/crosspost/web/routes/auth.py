"""Роут авторизации: вход, выход, «кто я». Часть 1.

Вход по login+паролю → подписанная httponly-кука с user_id (пароль в куке НЕ лежит).
Регистрации/восстановления нет — аккаунты заводит админ (routes/users, CLI user-add).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from crosspost.auth.passwords import hash_password, verify_password
from crosspost.auth.sessions import sign_session
from crosspost.db.models import Role
from crosspost.web.deps import SESSION_COOKIE, CurrentUser, UserRepoDep

router = APIRouter(prefix="/api/auth", tags=["auth"])

_SESSION_MAX_AGE = 30 * 24 * 3600  # 30 дней — совпадает с sessions._MAX_AGE

# Фиктивный хэш: сверяем пароль ДАЖЕ когда логина нет, чтобы время ответа не
# выдавало существование аккаунта (защита от перебора логинов).
_DUMMY_HASH = hash_password("crosspost-dummy-constant")


class LoginBody(BaseModel):
    login: str
    password: str


class MeOut(BaseModel):
    id: int
    login: str
    role: Role
    profile_id: int | None

    model_config = {"from_attributes": True}


@router.post("/login", response_model=MeOut)
async def login(body: LoginBody, response: Response, users: UserRepoDep) -> MeOut:
    user = await users.get_by_login(body.login.strip())
    # Всегда выполняем verify_password (по фиктивному хэшу, если юзера нет) —
    # постоянное время ответа, не раскрываем, какой логин существует.
    reference = user.password_hash if user is not None else _DUMMY_HASH
    ok = verify_password(body.password, reference)
    if user is None or not ok:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    response.set_cookie(
        SESSION_COOKIE,
        sign_session(user.id),
        max_age=_SESSION_MAX_AGE,
        httponly=True,  # JS не читает куку → токен не украсть XSS-ом
        samesite="lax",
        path="/",
    )
    return MeOut.model_validate(user)


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser) -> MeOut:
    return MeOut.model_validate(user)
