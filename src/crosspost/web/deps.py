"""FastAPI shared dependencies. Epic 4 + авторизация (роли/изоляция профилей)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.auth.sessions import verify_session
from crosspost.db.models import Role, User
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.user_repo import UserRepository
from crosspost.db.vault import get_vault

SESSION_COOKIE = "session"

# populated by lifespan in app.py
_session_factory: async_sessionmaker[AsyncSession] | None = None


def set_session_factory(factory: async_sessionmaker[AsyncSession]) -> None:
    global _session_factory
    _session_factory = factory


async def get_repo() -> AsyncGenerator[ProfileRepository, None]:
    assert _session_factory is not None, "Session factory not initialised"
    vault = get_vault()
    async with _session_factory() as session:
        yield ProfileRepository(session, vault=vault)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Сырая сессия — когда в роуте нужны несколько репозиториев на одной сессии."""
    assert _session_factory is not None, "Session factory not initialised"
    async with _session_factory() as session:
        yield session


async def get_user_repo() -> AsyncGenerator[UserRepository, None]:
    assert _session_factory is not None, "Session factory not initialised"
    async with _session_factory() as session:
        yield UserRepository(session)


RepoDep = Annotated[ProfileRepository, Depends(get_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserRepoDep = Annotated[UserRepository, Depends(get_user_repo)]


# ── Авторизация ────────────────────────────────────────────────────────────────


async def get_current_user(
    users: UserRepoDep,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    """Текущий пользователь из подписанной куки. 401, если входа нет/сессия невалидна.

    Кука httponly и подписана HMAC — подмена user_id ломает подпись (verify_session).
    """
    if not session_token:
        raise HTTPException(status_code=401, detail="Требуется вход")
    user_id = verify_session(session_token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Сессия недействительна, войдите заново")
    user = await users.get_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> User:
    """Разрешить только админу. 403 для client."""
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="Доступно только администратору")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def require_profile_access(profile_id: int, user: CurrentUser) -> User:
    """Гейт изоляции: client — только СВОЙ profile_id, admin — любой. Иначе 403.

    Вешается зависимостью на все profile-scoped роуты (channels/publish/tg_login,
    get_profile). Проверка на БЭКЕНДЕ — не полагаемся на то, что UI спрячет чужое.
    """
    if user.role == Role.ADMIN:
        return user
    if user.profile_id is None or user.profile_id != profile_id:
        raise HTTPException(status_code=403, detail="Доступ к чужому профилю запрещён")
    return user
