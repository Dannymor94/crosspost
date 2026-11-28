"""Bounded, opt-in initial Telegram connection; independent of legacy login slots."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from crosspost.channels import telegram_login as tl
from crosspost.channels.validators import TargetError, normalize_telegram_target
from crosspost.db.initial_connection import save_initial_connection
from crosspost.db.models import CredentialKind, Profile, Role, User
from crosspost.web.auth_guard import limit_auth, require_https
from crosspost.web.deps import SESSION_COOKIE, RepoDep, SessionDep, require_profile_access
from crosspost.web.private_routes import PrivateRoute

router = APIRouter(tags=["client-telegram"], route_class=PrivateRoute)
Actor = Annotated[User, Depends(require_profile_access)]
TTL = 300
MAX_FLOWS = 32
TIMEOUT = 25


def enabled() -> bool:
    return os.environ.get("CLIENT_TELEGRAM_CONNECTION_ENABLED", "") == "true"


@dataclass
class Flow:
    key: str
    profile_id: int
    created_at: datetime
    actor_id: int
    generation: int
    cookie_hash: str
    expires: float
    step: str = "starting"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    expiry_task: asyncio.Task[None] | None = None


_flows: dict[str, Flow] = {}


def _cookie_hash(request: Request) -> str:
    return hashlib.sha256(request.cookies.get(SESSION_COOKIE, "").encode()).hexdigest()


def _allow(actor: User, request: Request) -> None:
    require_https(request)
    if not enabled():
        raise HTTPException(503, "Самостоятельный вход пока недоступен")
    if actor.role != Role.CLIENT:
        raise HTTPException(403, "Только клиент своего профиля")


async def _close(flow: Flow) -> None:
    # Called under the flow lock. Remove admission only after disconnect.
    try:
        await asyncio.wait_for(tl._discard(flow.key), timeout=5)
    except Exception:
        pass
    finally:
        _flows.pop(flow.key, None)
        if flow.expiry_task is not None and flow.expiry_task is not asyncio.current_task():
            flow.expiry_task.cancel()


async def _expire(flow: Flow) -> None:
    await asyncio.sleep(max(0, flow.expires - time.monotonic()))
    async with flow.lock:
        if _flows.get(flow.key) is flow:
            await _close(flow)


async def close_all() -> None:
    for flow in list(_flows.values()):
        async with flow.lock:
            await _close(flow)


def _lookup(key: str, actor: User, profile_id: int, request: Request) -> Flow:
    flow = _flows.get(key)
    if (
        flow is None
        or flow.profile_id != profile_id
        or flow.actor_id != actor.id
        or flow.generation != actor.session_generation
        or flow.cookie_hash != _cookie_hash(request)
    ):
        raise HTTPException(409, "Вход устарел. Начните заново.")
    return flow


async def _check(flow: Flow, repo: RepoDep, actor: User) -> None:
    if _flows.get(flow.key) is not flow or time.monotonic() >= flow.expires:
        await _close(flow)
        raise HTTPException(409, "Время входа истекло")
    current = await repo.session.get(User, actor.id)
    profile = await repo.session.get(Profile, flow.profile_id)
    valid = (
        current is not None
        and current.role == Role.CLIENT
        and current.profile_id == flow.profile_id
        and current.session_generation == flow.generation
        and profile is not None
        and profile.created_at == flow.created_at
    )
    await repo.session.rollback()
    if not valid:
        await _close(flow)
        raise HTTPException(409, "Сеанс изменился")


class PrivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class BeginBody(PrivateBody):
    api_id: int = Field(gt=0, le=2147483647)
    api_hash: str = Field(pattern=r"^[a-fA-F0-9]{32}$")
    target: str = Field(min_length=1, max_length=255)


class StepBody(PrivateBody):
    flow: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    phone: str = Field(default="", max_length=24)
    code: str = Field(default="", max_length=12)
    password: str = Field(default="", max_length=256)


@router.post("/api/profiles/{profile_id}/client-telegram/begin")
async def begin(
    profile_id: int,
    body: BeginBody,
    actor: Actor,
    request: Request,
    repo: RepoDep,
    admission: SessionDep,
) -> dict[str, str]:
    _allow(actor, request)
    await limit_auth(request, admission, "tg_client_begin", str(actor.id))
    profile = await repo.get_profile(profile_id)
    if profile is None:
        raise HTTPException(404, "Профиль не найден")
    created_at = profile.created_at
    if (
        await repo.get_connection(profile_id, "telegram") is not None
        or await repo.get_credential(profile_id, "telegram", CredentialKind.API_TOKEN) is not None
    ):
        raise HTTPException(409, "Подключение уже существует")
    await repo.session.rollback()
    try:
        target = normalize_telegram_target(body.target)
    except TargetError:
        raise HTTPException(422, "Проверьте адрес канала") from None
    # No await between checking and reserving. One slot per profile, bounded globally.
    if len(_flows) >= MAX_FLOWS or any(f.profile_id == profile_id for f in _flows.values()):
        raise HTTPException(409, "Вход уже начат. Завершите его или дождитесь истечения срока.")
    key = secrets.token_urlsafe(32)
    flow = Flow(
        key,
        profile_id,
        created_at,
        actor.id,
        actor.session_generation,
        _cookie_hash(request),
        time.monotonic() + TTL,
    )
    _flows[key] = flow
    flow.expiry_task = asyncio.create_task(_expire(flow))
    async with flow.lock:
        try:
            await _check(flow, repo, actor)
            flow.step = await asyncio.wait_for(
                tl.begin(key, body.api_id, body.api_hash, target), timeout=TIMEOUT
            )
            if flow.step != "phone":
                raise ValueError("Unexpected step")
        except HTTPException:
            await _close(flow)
            raise
        except asyncio.CancelledError:
            await _close(flow)
            raise
        except Exception:
            await _close(flow)
            raise HTTPException(502, "Не удалось начать вход") from None
    return {"flow": key, "step": "phone"}


@router.post("/api/profiles/{profile_id}/client-telegram/{action}")
async def step(
    profile_id: int,
    action: Literal["phone", "code", "password", "cancel"],
    body: StepBody,
    actor: Actor,
    request: Request,
    repo: RepoDep,
    admission: SessionDep,
) -> dict[str, str]:
    _allow(actor, request)
    flow = _lookup(body.flow, actor, profile_id, request)
    if action != "cancel":
        await limit_auth(request, admission, "tg_client_" + action, str(actor.id))
    async with flow.lock:
        await _check(flow, repo, actor)
        if action == "cancel":
            await _close(flow)
            return {"step": "cancelled"}
        if action != flow.step:
            raise HTTPException(409, "Шаг входа уже изменился")
        try:
            if action == "phone":
                if (
                    not body.phone.startswith("+")
                    or not body.phone[1:].isascii()
                    or not body.phone[1:].isdecimal()
                ):
                    raise HTTPException(422, "Укажите международный номер")
                next_step = await asyncio.wait_for(
                    tl.request_code(flow.key, body.phone), timeout=TIMEOUT
                )
                if next_step != "code":
                    raise ValueError("Unexpected step")
                flow.step = next_step
                return {"step": next_step}
            if action == "code" and (not body.code.isascii() or not body.code.isdecimal()):
                raise HTTPException(422, "Проверьте код")
            if action == "password" and not body.password:
                raise HTTPException(422, "Введите пароль")
            result = await asyncio.wait_for(
                tl.submit_code(
                    flow.key,
                    code=body.code if action == "code" else None,
                    password=body.password if action == "password" else None,
                ),
                timeout=TIMEOUT,
            )
            if result.step == "password":
                flow.step = "password"
                return {"step": "password"}
            if result.step != "done" or not all(
                (result.api_id, result.api_hash, result.target_channel, result.session_string)
            ):
                raise ValueError("Unexpected result")
            if time.monotonic() >= flow.expires:
                raise HTTPException(409, "Время входа истекло")
            blob = tl.build_credential_blob(
                api_id=result.api_id or 0,
                api_hash=result.api_hash or "",
                target_channel=result.target_channel or "",
                session=result.session_string or "",
            )
            if not await save_initial_connection(
                repo,
                profile_id=profile_id,
                profile_created_at=flow.created_at,
                actor_id=flow.actor_id,
                generation=flow.generation,
                channel="telegram",
                plaintext=blob,
                expires_at=None,
            ):
                raise HTTPException(409, "Состояние изменилось. Обновите страницу.")
            await _close(flow)
            return {"step": "done", "state": "live"}
        except HTTPException:
            await _close(flow)
            raise
        except asyncio.CancelledError:
            await _close(flow)
            raise
        except Exception:
            await _close(flow)
            raise HTTPException(422, "Вход не подтверждён. Начните заново позже.") from None
