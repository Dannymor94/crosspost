"""Роут интерактивного входа в Telegram. Итерация 1 / Слой 1.

Многошаговый флоу через crosspost.channels.telegram_login:
  begin      → POST .../telegram/login/begin      {api_id, api_hash, target_channel}
  send-code  → POST .../telegram/login/send-code  {phone}
  sign-in    → POST .../telegram/login/sign-in     {code?, password?}
  cancel     → POST .../telegram/login/cancel

Серверное состояние флоу держит telegram_login (in-memory). Учётку в БД пишет
только этот роут (build_credential_blob → repo.set_credential, шифрует vault).
Телефон/пароль в БД не попадают — только итоговый StringSession + конфиг канала.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from crosspost.channels import telegram_login as tl
from crosspost.db.models import ConnectionState, CredentialKind
from crosspost.web.deps import RepoDep

router = APIRouter(tags=["telegram-login"])

_CHANNEL = "telegram"


# ── Схемы ─────────────────────────────────────────────────────────────────────


class BeginBody(BaseModel):
    api_id: int
    api_hash: str
    target_channel: str


class SendCodeBody(BaseModel):
    phone: str


class SignInBody(BaseModel):
    code: str | None = None
    password: str | None = None


class LoginStep(BaseModel):
    step: str  # "phone" | "code" | "password" | "done"
    state: str | None = None  # "live" когда step == "done"
    message: str = ""


# ── Хелперы ───────────────────────────────────────────────────────────────────


async def _require_profile(repo: RepoDep, profile_id: int) -> None:
    if await repo.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")


# ── Эндпоинты ─────────────────────────────────────────────────────────────────


@router.post("/api/profiles/{profile_id}/channels/telegram/login/begin", response_model=LoginStep)
async def login_begin(profile_id: int, body: BeginBody, repo: RepoDep) -> LoginStep:
    await _require_profile(repo, profile_id)
    try:
        step = await tl.begin(
            profile_id,
            api_id=body.api_id,
            api_hash=body.api_hash,
            target_channel=body.target_channel,
        )
    except tl.TelegramLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LoginStep(step=step, message="Введите номер телефона для получения кода.")


@router.post(
    "/api/profiles/{profile_id}/channels/telegram/login/send-code", response_model=LoginStep
)
async def login_send_code(profile_id: int, body: SendCodeBody, repo: RepoDep) -> LoginStep:
    await _require_profile(repo, profile_id)
    try:
        step = await tl.request_code(profile_id, body.phone)
    except tl.TelegramLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LoginStep(step=step, message="Код отправлен в Telegram. Введите его.")


@router.post("/api/profiles/{profile_id}/channels/telegram/login/sign-in", response_model=LoginStep)
async def login_sign_in(profile_id: int, body: SignInBody, repo: RepoDep) -> LoginStep:
    await _require_profile(repo, profile_id)
    try:
        result = await tl.submit_code(profile_id, code=body.code, password=body.password)
    except tl.TelegramLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result.step == "password":
        return LoginStep(
            step="password",
            message="Включён облачный пароль (2FA). Введите его.",
        )

    # step == "done": сохраняем учётку (StringSession + конфиг) и ставим live.
    blob = tl.build_credential_blob(
        api_id=result.api_id or 0,
        api_hash=result.api_hash or "",
        target_channel=result.target_channel or "",
        session=result.session_string or "",
    )
    await repo.set_credential(profile_id, _CHANNEL, CredentialKind.API_TOKEN, blob)
    await repo.upsert_connection(profile_id, _CHANNEL, ConnectionState.LIVE)
    return LoginStep(step="done", state=str(ConnectionState.LIVE), message="Telegram подключён.")


@router.post("/api/profiles/{profile_id}/channels/telegram/login/cancel", response_model=LoginStep)
async def login_cancel(profile_id: int, repo: RepoDep) -> LoginStep:
    await _require_profile(repo, profile_id)
    await tl._discard(profile_id)
    return LoginStep(step="cancelled", message="Вход отменён.")
