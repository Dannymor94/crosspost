"""Authentication admission and explicit HTTPS deployment policy."""

from __future__ import annotations

import ipaddress
import os

from fastapi import HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.auth.sessions import rate_key
from crosspost.db.auth_attempt_repo import SUBJECT_WINDOW, admit_attempt


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


async def limit_auth(
    request: Request, session: AsyncSession, family: str, subject: str
) -> None:
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
