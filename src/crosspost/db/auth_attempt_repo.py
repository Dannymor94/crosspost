"""Serialized admission before expensive password work; SQLite-only MVP."""

from __future__ import annotations

import time

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import AuthAttempt

IP_WINDOW = 900
SUBJECT_WINDOW = 3600
IP_LIMIT = 5
SUBJECT_LIMIT = 20
GLOBAL_LIMIT = 300


async def admit_attempt(
    session: AsyncSession, family: str, ip_key: str, subject_key: str, *, now: int | None = None
) -> bool:
    # No read snapshot before the writer lock. No password hashing under lock.
    await session.execute(text("BEGIN IMMEDIATE"))
    timestamp = int(time.time()) if now is None else now
    await session.execute(delete(AuthAttempt).where(AuthAttempt.occurred_at <= timestamp - 3600))
    family_count = select(func.count()).select_from(AuthAttempt).where(AuthAttempt.family == family)
    ip_count = await session.scalar(
        family_count.where(
            AuthAttempt.ip_key == ip_key, AuthAttempt.occurred_at > timestamp - IP_WINDOW
        )
    )
    subject_count = await session.scalar(
        family_count.where(
            AuthAttempt.subject_key == subject_key,
            AuthAttempt.occurred_at > timestamp - SUBJECT_WINDOW,
        )
    )
    total = await session.scalar(
        family_count.where(AuthAttempt.occurred_at > timestamp - IP_WINDOW)
    )
    if ip_count is None or subject_count is None or total is None:
        await session.rollback()
        return False
    if ip_count >= IP_LIMIT or subject_count >= SUBJECT_LIMIT or total >= GLOBAL_LIMIT:
        await session.rollback()
        return False
    session.add(
        AuthAttempt(family=family, ip_key=ip_key, subject_key=subject_key, occurred_at=timestamp)
    )
    await session.commit()
    return True
