"""Authentication admission and explicit HTTPS deployment policy."""

from __future__ import annotations

import asyncio
import ipaddress
import os

from fastapi import HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.auth.passwords import verify_password
from crosspost.auth.sessions import rate_key
from crosspost.db.auth_attempt_repo import SUBJECT_WINDOW, admit_attempt
from crosspost.db.models import Role, User
from crosspost.db.owner_mfa import consume_owner_factor


def production_mode() -> bool:
    value = os.environ.get("CROSSPOST_AUTH_MODE", "development").strip().lower()
    if value not in {"development", "production"}:
        raise HTTPException(503, "Авторизация недоступна")
    return value == "production"


def require_https(request: Request) -> bool:
    secure = request.url.scheme == "https"
    if production_mode() and not secure:
        raise HTTPException(503, "Требуется защищённое соединение")
    return secure


def _peer(request: Request) -> str:
    # Uvicorn resolves forwarded headers only at the separately reviewed proxy
    # boundary. Never read arbitrary X-Forwarded-For from the request here.
    try:
        return str(ipaddress.ip_address(request.client.host if request.client else ""))
    except ValueError:
        return "unknown"


async def limit_auth(request: Request, session: AsyncSession, family: str, subject: str) -> None:
    require_https(request)
    enabled = production_mode() or (
        os.environ.get("CROSSPOST_AUTH_RATE_LIMIT", "").strip().lower() == "true"
    )
    if not enabled:
        return
    try:
        allowed = await admit_attempt(
            session,
            family,
            rate_key(family, "ip", _peer(request)),
            rate_key(family, "subject", subject),
        )
    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(503, "Авторизация временно недоступна") from None
    if not allowed:
        raise HTTPException(
            429,
            "Слишком много попыток. Попробуйте позже.",
            headers={"Retry-After": str(SUBJECT_WINDOW)},
        )


async def begin_owner_step_up(
    request: Request,
    session: AsyncSession,
    admin: User,
    password: str,
    code: str,
) -> None:
    """Hold writer lock through the caller's mutation and audit commit."""
    await limit_auth(request, session, "owner_step_up", str(admin.id))
    if not await asyncio.to_thread(verify_password, password, admin.password_hash):
        raise HTTPException(403, "Подтвердите пароль администратора")
    if admin.mfa_secret is not None:
        accepted = await consume_owner_factor(
            session, admin.id, admin.session_generation, admin.password_hash, code, commit=False
        )
        if not accepted:
            raise HTTPException(403, "Требуется новый код двухфакторного входа")
        return
    if production_mode():
        raise HTTPException(403, "Требуется настройка двухфакторного входа")
    # Development-only compatibility; still recheck generation under writer lock.
    await session.execute(text("BEGIN IMMEDIATE"))
    uid = await session.scalar(
        select(User.id).where(
            User.id == admin.id,
            User.role == Role.ADMIN,
            User.session_generation == admin.session_generation,
            User.password_hash == admin.password_hash,
            User.mfa_secret.is_(None),
        )
    )
    if uid is None:
        await session.rollback()
        raise HTTPException(403, "Сеанс владельца изменился")
