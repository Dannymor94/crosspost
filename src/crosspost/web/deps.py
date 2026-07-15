"""FastAPI shared dependencies. Epic 4."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.vault import get_vault

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


RepoDep = Annotated[ProfileRepository, Depends(get_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
