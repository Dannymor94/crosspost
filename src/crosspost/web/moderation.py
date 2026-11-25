"""Fail-closed guard for client actions outside the moderated schedule flow."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.models import Role, User
from crosspost.db.settings_repo import ProfileSettingsRepository


async def require_unmoderated_action(
    user: User, session: AsyncSession, profile_id: int
) -> None:
    """Reject direct actions when the profile requires client approval.

    Call only after profile access has been checked. The existing scheduled
    creation endpoint is the supported path to PENDING_APPROVAL.
    """
    if user.role != Role.CLIENT:
        return
    settings = ProfileSettingsRepository(session, profile_id=profile_id)
    if await settings.get_client_moderation():
        raise HTTPException(
            status_code=403,
            detail="Для клиента включена модерация. Создайте пост на проверку.",
        )
