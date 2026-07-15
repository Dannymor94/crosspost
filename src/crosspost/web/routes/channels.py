"""Channels router. Epic 4."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from crosspost.channels.connection_validator import validate_connection
from crosspost.channels.validators import VALIDATORS
from crosspost.db.models import ConnectionState, CredentialKind
from crosspost.web.deps import RepoDep

logger = logging.getLogger(__name__)
router = APIRouter(tags=["channels"])

# ── Channel field definitions (for frontend dynamic forms) ────────────────────

# Поля шага 1 интерактивного входа Telegram (сам вход — через /telegram/login/*).
CHANNEL_FIELDS: dict[str, list[dict]] = {
    "telegram": [
        {
            "name": "api_id",
            "label": "api_id",
            "hint": "my.telegram.org → API development tools → App api_id",
            "link": "https://my.telegram.org/apps",
            "secret": False,
        },
        {
            "name": "api_hash",
            "label": "api_hash",
            "hint": "my.telegram.org → API development tools → App api_hash",
            "link": "https://my.telegram.org/apps",
            "secret": True,
        },
        {
            "name": "target_channel",
            "label": "Канал назначения",
            "hint": "@username канала, куда публикуем (вы должны быть админом)",
            "link": "",
            "secret": False,
        },
    ],
    "vk": [
        {
            "name": "token",
            "label": "Токен VK",
            "hint": "Настройки → Безопасность → Токены доступа",
            "link": "",
            "secret": True,
        }
    ],
}

# Maps channel → (field_name_in_fields_dict → CredentialKind).
# Только для НЕинтерактивных API-каналов (/connect). Telegram сюда не входит —
# у него многошаговый вход через /telegram/login/*.
_FIELD_TO_CREDENTIAL: dict[str, dict[str, CredentialKind]] = {
    "vk": {"token": CredentialKind.API_TOKEN},
}


# ── Schemas ───────────────────────────────────────────────────────────────────


class ChannelStatusOut(BaseModel):
    channel: str
    title: str
    kind: str
    state: str
    has_credential: bool
    interactive: bool
    needs_target: bool = False
    target_label: str = ""
    target_hint: str = ""
    target: str = ""  # текущая per-profile цель (не секрет — адрес группы/орг)


class TargetBody(BaseModel):
    target: str


class ChannelRegistryItem(BaseModel):
    channel: str
    title: str
    kind: str
    credential_kind: str
    interactive: bool


class ChannelFieldDef(BaseModel):
    name: str
    label: str
    hint: str
    secret: bool
    link: str = ""


class ConnectBody(BaseModel):
    fields: dict[str, str]


class ValidationResult(BaseModel):
    state: str
    message: str


# ── Registry endpoint ─────────────────────────────────────────────────────────


@router.get("/api/channels/registry", response_model=list[ChannelRegistryItem])
async def get_registry() -> list[ChannelRegistryItem]:
    return [
        ChannelRegistryItem(
            channel=ch,
            title=v.title or ch,
            kind=v.kind,
            credential_kind=str(v.credential_kind),
            interactive=v.interactive,
        )
        for ch, v in VALIDATORS.items()
        if v.enabled
    ]


@router.get("/api/channels/{channel}/fields", response_model=list[ChannelFieldDef])
async def get_channel_fields(channel: str) -> list[ChannelFieldDef]:
    if channel not in VALIDATORS:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not in registry")
    fields = CHANNEL_FIELDS.get(channel, [])
    return [ChannelFieldDef(**f) for f in fields]


# ── Profile-scoped channel endpoints ─────────────────────────────────────────


@router.get(
    "/api/profiles/{profile_id}/channels",
    response_model=list[ChannelStatusOut],
)
async def list_channel_statuses(profile_id: int, repo: RepoDep) -> list[ChannelStatusOut]:
    profile = await repo.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    connections = await repo.get_connections(profile_id)
    conn_map = {c.channel: c.state for c in connections}

    result = []
    for ch, v in VALIDATORS.items():
        if not v.enabled:
            continue
        state = conn_map.get(ch)
        state_str = str(state) if state is not None else "not_connected"

        cred = await repo.get_credential(profile_id, ch, v.credential_kind)
        has_cred = cred is not None

        target = ""
        if v.needs_target:
            target = await repo.get_credential(profile_id, ch, CredentialKind.TARGET) or ""

        result.append(
            ChannelStatusOut(
                channel=ch,
                title=v.title or ch,
                kind=v.kind,
                state=state_str,
                has_credential=has_cred,
                interactive=v.interactive,
                needs_target=v.needs_target,
                target_label=v.target_label,
                target_hint=v.target_hint,
                target=target,
            )
        )
    return result


@router.get(
    "/api/profiles/{profile_id}/channels/{channel}/target",
    response_model=TargetBody,
)
async def get_target(profile_id: int, channel: str, repo: RepoDep) -> TargetBody:
    if channel not in VALIDATORS:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not in registry")
    target = await repo.get_credential(profile_id, channel, CredentialKind.TARGET)
    return TargetBody(target=target or "")


@router.put(
    "/api/profiles/{profile_id}/channels/{channel}/target",
    response_model=TargetBody,
)
async def set_target(
    profile_id: int, channel: str, body: TargetBody, repo: RepoDep
) -> TargetBody:
    """Сохранить per-profile цель постинга (группа/организация). Изоляция клиентов."""
    if await repo.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    v = VALIDATORS.get(channel)
    if v is None or not v.needs_target:
        raise HTTPException(status_code=400, detail="Канал не требует цели постинга")
    target = body.target.strip()
    if not target:
        raise HTTPException(status_code=422, detail="Укажите цель (группу/организацию)")
    await repo.set_credential(profile_id, channel, CredentialKind.TARGET, target)
    return TargetBody(target=target)


@router.post(
    "/api/profiles/{profile_id}/channels/{channel}/connect",
    response_model=ValidationResult,
)
async def connect_channel(
    profile_id: int,
    channel: str,
    body: ConnectBody,
    repo: RepoDep,
) -> ValidationResult:
    profile = await repo.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    validator = VALIDATORS.get(channel)
    if validator is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not in registry")

    if validator.kind == "browser":
        raise HTTPException(
            status_code=400,
            detail="Browser-tier channels use the /browser-login/* flow, not /connect",
        )

    if validator.interactive:
        raise HTTPException(
            status_code=400,
            detail="Interactive channels use the /telegram/login/* flow, not /connect",
        )

    field_map = _FIELD_TO_CREDENTIAL.get(channel, {})
    if not field_map:
        raise HTTPException(status_code=422, detail=f"No field mapping defined for '{channel}'")

    for field_name, cred_kind in field_map.items():
        value = body.fields.get(field_name)
        if not value:
            raise HTTPException(
                status_code=422, detail=f"Missing field '{field_name}' for channel '{channel}'"
            )
        await repo.set_credential(profile_id, channel, cred_kind, value)

    state = await validate_connection(repo, profile_id, channel)
    msg = (
        "Connected and validated"
        if state == ConnectionState.LIVE
        else "Saved but validation failed"
    )
    return ValidationResult(state=str(state), message=msg)


@router.post(
    "/api/profiles/{profile_id}/channels/{channel}/validate",
    response_model=ValidationResult,
)
async def validate_channel(
    profile_id: int,
    channel: str,
    repo: RepoDep,
) -> ValidationResult:
    profile = await repo.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    if channel not in VALIDATORS:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not in registry")

    state = await validate_connection(repo, profile_id, channel)
    msg = "Connection is live" if state == ConnectionState.LIVE else "Connection needs relogin"
    return ValidationResult(state=str(state), message=msg)


def _require_browser_channel(profile, channel: str):
    """Проверить, что профиль есть и канал браузерный. Вернуть валидатор."""
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    validator = VALIDATORS.get(channel)
    if validator is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not in registry")
    if validator.kind != "browser":
        raise HTTPException(status_code=400, detail="Только браузерные каналы имеют вход в окне")
    return validator


def _channels_sharing_session(session_key: str) -> set[str]:
    """Все включённые каналы, использующие эту сессию (session_channel или сам канал).

    Для vk_wall вернёт {vk_wall, vk_channel} — они делят один VK-аккаунт.
    """
    return {
        ch for ch, v in VALIDATORS.items() if v.enabled and (v.session_channel or ch) == session_key
    }


@router.post(
    "/api/profiles/{profile_id}/channels/{channel}/disconnect",
    response_model=ValidationResult,
)
async def disconnect_channel(profile_id: int, channel: str, repo: RepoDep) -> ValidationResult:
    """Сбросить вход: удалить учётку канала у профиля и вернуть его в «не подключён».

    Учётка (StringSession / storageState) хранится по session_key. Для каналов,
    делящих аккаунт (vk_channel ↔ vk_wall), сброс одной сбрасывает общую VK-сессию —
    оба канала становятся «не подключён» (иначе состояние рассинхронится).
    """
    profile = await repo.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    validator = VALIDATORS.get(channel)
    if validator is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not in registry")

    # Закрыть возможное открытое окно браузерного входа.
    if validator.kind == "browser":
        from crosspost.channels import browser_login as bl  # noqa: PLC0415

        await bl.cancel(profile_id, channel)

    session_key = validator.session_channel or channel
    await repo.delete_credential(profile_id, session_key, validator.credential_kind)

    # Снять подключения всех каналов, деливших эту сессию (+ сам канал на всякий).
    for ch in _channels_sharing_session(session_key) | {channel}:
        await repo.delete_connection(profile_id, ch)

    return ValidationResult(state="not_connected", message="Вход сброшен")


@router.post(
    "/api/profiles/{profile_id}/channels/{channel}/browser-login/begin",
    response_model=ValidationResult,
)
async def browser_login_begin(profile_id: int, channel: str, repo: RepoDep) -> ValidationResult:
    """Открыть браузер со СВЕЖИМ пустым контекстом на странице входа канала."""
    _require_browser_channel(await repo.get_profile(profile_id), channel)

    from crosspost.channels import browser_login as bl  # noqa: PLC0415 — ленивый (Playwright)

    try:
        await bl.begin(profile_id, channel)
    except bl.BrowserLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("browser login begin failed %s/%s: %s", profile_id, channel, exc)
        raise HTTPException(status_code=500, detail=f"Не удалось открыть браузер: {exc}") from exc

    return ValidationResult(
        state="opening",
        message="Войдите в открывшемся окне браузера, затем нажмите «Я вошёл».",
    )


@router.post(
    "/api/profiles/{profile_id}/channels/{channel}/browser-login/confirm",
    response_model=ValidationResult,
)
async def browser_login_confirm(profile_id: int, channel: str, repo: RepoDep) -> ValidationResult:
    """Пользователь подтвердил вход — ПОЗИТИВНО проверяем и сохраняем сессию профиля."""
    validator = _require_browser_channel(await repo.get_profile(profile_id), channel)

    from crosspost.channels import browser_login as bl  # noqa: PLC0415

    try:
        state_dict = await bl.confirm(profile_id, channel)
    except bl.BrowserLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if state_dict is None:
        # Вход ещё не виден — окно оставлено открытым, пользователь может дожать.
        raise HTTPException(
            status_code=409,
            detail="Вход ещё не завершён. Войдите в окне до рабочего кабинета и повторите.",
        )

    # Сессия per-profile: пишем под session_key (vk_channel делит с vk_wall).
    session_key = validator.session_channel or channel
    await repo.set_credential(
        profile_id, session_key, CredentialKind.STORAGE_STATE, json.dumps(state_dict)
    )

    state = await validate_connection(repo, profile_id, channel)
    msg = "Вход выполнен, канал активен" if state == ConnectionState.LIVE else "Вход сохранён"
    return ValidationResult(state=str(state), message=msg)


@router.post(
    "/api/profiles/{profile_id}/channels/{channel}/browser-login/cancel",
    response_model=ValidationResult,
)
async def browser_login_cancel(profile_id: int, channel: str, repo: RepoDep) -> ValidationResult:
    """Закрыть окно входа, ничего не сохранять."""
    _require_browser_channel(await repo.get_profile(profile_id), channel)

    from crosspost.channels import browser_login as bl  # noqa: PLC0415

    await bl.cancel(profile_id, channel)
    return ValidationResult(state="cancelled", message="Вход отменён.")
