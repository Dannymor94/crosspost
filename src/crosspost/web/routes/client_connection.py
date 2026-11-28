"""Opt-in client-owned initial API connection, validation before persistence."""

from __future__ import annotations

import json
import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from crosspost.channels.validators import VALIDATORS, ValidatorResult
from crosspost.db.initial_connection import save_initial_connection
from crosspost.db.models import Role, User
from crosspost.web.auth_guard import limit_auth
from crosspost.web.deps import RepoDep, SessionDep, require_profile_access
from crosspost.web.private_routes import PrivateRoute

router = APIRouter(tags=["client-connection"], route_class=PrivateRoute)
Actor = Annotated[User, Depends(require_profile_access)]


def _enabled() -> bool:
    return os.environ.get("CLIENT_API_CONNECTION_ENABLED", "") == "true"


class ConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    token: str = Field(min_length=1, max_length=8192)
    ig_user_id: str = Field(default="", max_length=32)


@router.get("/api/profiles/{profile_id}/client-connection/capabilities")
async def capabilities(profile_id: int, actor: Actor) -> dict[str, list[str]]:
    return {
        "channels": ["telegraph", "instagram"] if _enabled() and actor.role == Role.CLIENT else []
    }


@router.post("/api/profiles/{profile_id}/client-connection/{channel}")
async def connect(
    profile_id: int,
    channel: Literal["telegraph", "instagram"],
    body: ConnectBody,
    actor: Actor,
    request: Request,
    repo: RepoDep,
    admission: SessionDep,
) -> dict[str, str]:
    if not _enabled():
        raise HTTPException(503, "Самостоятельное подключение пока недоступно")
    if actor.role != Role.CLIENT:
        raise HTTPException(403, "Только клиент своего профиля")
    await limit_auth(request, admission, "client_connection", str(actor.id))
    profile = await repo.get_profile(profile_id)
    if profile is None:
        raise HTTPException(404, "Профиль не найден")
    created_at = profile.created_at
    if (
        await repo.get_connection(profile_id, channel) is not None
        or await repo.get_credential(profile_id, channel, VALIDATORS[channel].credential_kind)
        is not None
    ):
        raise HTTPException(409, "Подключение уже существует. Обратитесь к администратору.")
    await repo.session.rollback()
    token = body.token.strip()
    if not token or any(char.isspace() for char in token):
        raise HTTPException(422, "Проверьте токен")
    if channel == "instagram":
        if not body.ig_user_id.isascii() or not body.ig_user_id.isdecimal():
            raise HTTPException(422, "Проверьте идентификатор Instagram")
        plaintext = json.dumps({"ig_user_id": body.ig_user_id, "access_token": token})
    else:
        if body.ig_user_id:
            raise HTTPException(422, "Лишнее поле")
        plaintext = token
    try:
        result = await VALIDATORS[channel].fn(plaintext)
    except Exception:
        raise HTTPException(503, "Не удалось проверить подключение") from None
    if not isinstance(result, ValidatorResult) or not result.alive:
        raise HTTPException(422, "Площадка не подтвердила подключение. Данные не сохранены.")
    if not await save_initial_connection(
        repo,
        profile_id=profile_id,
        profile_created_at=created_at,
        actor_id=actor.id,
        generation=actor.session_generation,
        channel=channel,
        plaintext=plaintext,
        expires_at=result.expires_at,
    ):
        raise HTTPException(409, "Состояние изменилось. Обновите страницу.")
    return {"state": "live"}
