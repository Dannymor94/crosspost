"""Profiles router. Epic 4."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from crosspost.web.deps import RepoDep

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


class ProfileOut(BaseModel):
    id: int
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ProfileCreate(BaseModel):
    name: str


@router.get("", response_model=list[ProfileOut])
async def list_profiles(repo: RepoDep) -> list[ProfileOut]:
    profiles = await repo.list_profiles()
    return [ProfileOut.model_validate(p) for p in profiles]


@router.post("", response_model=ProfileOut, status_code=201)
async def create_profile(body: ProfileCreate, repo: RepoDep) -> ProfileOut:
    try:
        profile = await repo.create_profile(body.name)
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail=f"Profile '{body.name}' already exists"
        ) from exc
    return ProfileOut.model_validate(profile)


@router.get("/{profile_id}", response_model=ProfileOut)
async def get_profile(profile_id: int, repo: RepoDep) -> ProfileOut:
    profile = await repo.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return ProfileOut.model_validate(profile)
