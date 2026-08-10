"""Profiles router. Epic 4."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from crosspost.db.models import Role
from crosspost.web.deps import AdminUser, CurrentUser, RepoDep, require_profile_access

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


class ProfileOut(BaseModel):
    id: int
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ProfileCreate(BaseModel):
    name: str


@router.get("", response_model=list[ProfileOut])
async def list_profiles(user: CurrentUser, repo: RepoDep) -> list[ProfileOut]:
    """admin → все профили; client → только свой (один или пусто)."""
    if user.role == Role.ADMIN:
        profiles = await repo.list_profiles()
    else:
        own = await repo.get_profile(user.profile_id) if user.profile_id else None
        profiles = [own] if own is not None else []
    return [ProfileOut.model_validate(p) for p in profiles]


@router.post("", response_model=ProfileOut, status_code=201)
async def create_profile(body: ProfileCreate, _: AdminUser, repo: RepoDep) -> ProfileOut:
    """Создание профиля — только админ (клиенты не заводят профили)."""
    try:
        profile = await repo.create_profile(body.name)
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail=f"Profile '{body.name}' already exists"
        ) from exc
    return ProfileOut.model_validate(profile)


@router.get(
    "/{profile_id}",
    response_model=ProfileOut,
    dependencies=[Depends(require_profile_access)],
)
async def get_profile(profile_id: int, repo: RepoDep) -> ProfileOut:
    """Один профиль. Гейт require_profile_access: client — только свой, иначе 403."""
    profile = await repo.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return ProfileOut.model_validate(profile)
