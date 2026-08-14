"""Channels router. Epic 4."""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from crosspost.channels.connection_validator import validate_connection
from crosspost.channels.guide import target_value_field
from crosspost.channels.validators import VALIDATORS, normalize_target
from crosspost.db.models import ConnectionState, CredentialKind
from crosspost.db.target_repo import ChannelTargetRepository
from crosspost.db.vault import get_vault
from crosspost.web.deps import RepoDep, SessionDep, require_profile_access

logger = logging.getLogger(__name__)
router = APIRouter(tags=["channels"])

# Гейт изоляции для profile-scoped роутов (client — только свой profile_id → иначе 403).
# Реестр каналов и поля формы (/api/channels/*) — НЕ per-profile, остаются открытыми
# для любого вошедшего (метаданные каналов, без данных клиента).
_PROFILE_GUARD = [Depends(require_profile_access)]

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
    "telegraph": [
        {
            "name": "token",
            "label": "Токен Telegraph",
            "hint": "Технический токен доступа — если его нет, напишите мне, создам",
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
    "telegraph": {"token": CredentialKind.API_TOKEN},
}


# ── Schemas ───────────────────────────────────────────────────────────────────


class GuideBlockOut(BaseModel):
    label: str
    steps: list[str]
    link: str = ""
    link_label: str = ""
    field: str = ""


class StepHintOut(BaseModel):
    key: str
    text: str


class ErrorHintOut(BaseModel):
    symptom: str
    check: str


class WizardFieldOut(BaseModel):
    name: str
    label: str
    placeholder: str = ""
    secret: bool = False
    hint: str = ""
    pattern: str = ""
    invalid_message: str = ""
    normalize: str = "trim"
    paste: bool = True
    required: bool = True


class WizardStepOut(BaseModel):
    key: str
    kind: str
    title: str
    body: str = ""
    landmark: str = ""
    link: str = ""
    link_label: str = ""
    fields: list[WizardFieldOut] = []
    optional: bool = False
    resend_label: str = ""
    alt_step: str = ""
    alt_label: str = ""


class ChannelGuideOut(BaseModel):
    intro: str = ""
    blocks: list[GuideBlockOut] = []
    step_hints: list[StepHintOut] = []
    browser_note: str = ""
    errors: list[ErrorHintOut] = []
    steps: list[WizardStepOut] = []


def _wizard_field_out(f) -> WizardFieldOut:
    return WizardFieldOut(
        name=f.name,
        label=f.label,
        placeholder=f.placeholder,
        secret=f.secret,
        hint=f.hint,
        pattern=f.pattern,
        invalid_message=f.invalid_message,
        normalize=f.normalize,
        paste=f.paste,
        required=f.required,
    )


def _target_field_out(guide) -> WizardFieldOut | None:
    """Декларация поля-значения цели (из мастера) → сериализуемый вид или None."""
    f = target_value_field(guide)
    return _wizard_field_out(f) if f is not None else None


def _guide_out(guide) -> ChannelGuideOut | None:
    """Декларация инструкции (dataclass) → сериализуемый вид для UI."""
    if guide is None:
        return None
    return ChannelGuideOut(
        intro=guide.intro,
        blocks=[
            GuideBlockOut(
                label=b.label,
                steps=list(b.steps),
                link=b.link,
                link_label=b.link_label,
                field=b.field,
            )
            for b in guide.blocks
        ],
        step_hints=[StepHintOut(key=s.key, text=s.text) for s in guide.step_hints],
        browser_note=guide.browser_note,
        errors=[ErrorHintOut(symptom=e.symptom, check=e.check) for e in guide.errors],
        steps=[
            WizardStepOut(
                key=s.key,
                kind=s.kind,
                title=s.title,
                body=s.body,
                landmark=s.landmark,
                link=s.link,
                link_label=s.link_label,
                optional=s.optional,
                resend_label=s.resend_label,
                alt_step=s.alt_step,
                alt_label=s.alt_label,
                fields=[
                    WizardFieldOut(
                        name=f.name,
                        label=f.label,
                        placeholder=f.placeholder,
                        secret=f.secret,
                        hint=f.hint,
                        pattern=f.pattern,
                        invalid_message=f.invalid_message,
                        normalize=f.normalize,
                        paste=f.paste,
                        required=f.required,
                    )
                    for f in s.fields
                ],
            )
            for s in guide.steps
        ],
    )


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
    target_field: WizardFieldOut | None = None  # декларация поля цели (для формы мультицелей)
    guide: ChannelGuideOut | None = None  # инструкция подключения (из декларации)


class TargetBody(BaseModel):
    target: str


class ChannelTargetOut(BaseModel):
    """Одна цель канала (мультицели). value — нормализованное значение (не секрет)."""

    id: int
    name: str
    value: str


class TargetCreateBody(BaseModel):
    name: str
    value: str


class TargetPatchBody(BaseModel):
    name: str | None = None
    value: str | None = None


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


class TelegraphCreateBody(BaseModel):
    short_name: str
    author_name: str
    author_url: str = ""


class TelegraphCreateOut(BaseModel):
    author_name: str
    author_url: str


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


@router.get("/api/channels/{channel}/guide", response_model=ChannelGuideOut)
async def get_channel_guide(channel: str) -> ChannelGuideOut:
    """Инструкция подключения канала (из декларации). 404 — канал/инструкции нет."""
    v = VALIDATORS.get(channel)
    if v is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not in registry")
    out = _guide_out(v.guide)
    if out is None:
        raise HTTPException(status_code=404, detail=f"No guide for channel '{channel}'")
    return out


# ── Profile-scoped channel endpoints ─────────────────────────────────────────


@router.get(
    "/api/profiles/{profile_id}/channels",
    response_model=list[ChannelStatusOut],
    dependencies=_PROFILE_GUARD,
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
            raw_target = await repo.get_credential(profile_id, ch, CredentialKind.TARGET) or ""
            target = normalize_target(ch, raw_target)  # показываем чистое значение

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
                target_field=_target_field_out(v.guide),
                guide=_guide_out(v.guide),
            )
        )
    return result


@router.get(
    "/api/profiles/{profile_id}/channels/{channel}/target",
    response_model=TargetBody,
    dependencies=_PROFILE_GUARD,
)
async def get_target(profile_id: int, channel: str, repo: RepoDep) -> TargetBody:
    if channel not in VALIDATORS:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not in registry")
    target = await repo.get_credential(profile_id, channel, CredentialKind.TARGET)
    return TargetBody(target=normalize_target(channel, target or ""))


@router.put(
    "/api/profiles/{profile_id}/channels/{channel}/target",
    response_model=TargetBody,
    dependencies=_PROFILE_GUARD,
)
async def set_target(profile_id: int, channel: str, body: TargetBody, repo: RepoDep) -> TargetBody:
    """Сохранить per-profile цель постинга (группа/организация). Изоляция клиентов."""
    if await repo.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    v = VALIDATORS.get(channel)
    if v is None or not v.needs_target:
        raise HTTPException(status_code=400, detail="Канал не требует цели постинга")
    raw = body.target.strip()
    if not raw:
        raise HTTPException(status_code=422, detail="Укажите цель (группу/организацию)")
    # Нормализуем: из полного URL/адреса вытягиваем чистое значение (screen_name/id).
    try:
        target = v.target_normalizer(raw) if v.target_normalizer else raw
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await repo.set_credential(profile_id, channel, CredentialKind.TARGET, target)
    return TargetBody(target=target)


# ── Мультицели канала (список именованных целей) ──────────────────────────────


def _require_target_channel(channel: str):
    """Валидатор канала, поддерживающего цели. 404 — нет канала, 400 — канал без целей."""
    v = VALIDATORS.get(channel)
    if v is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not in registry")
    if not v.needs_target:
        raise HTTPException(status_code=400, detail="Канал не имеет целей публикации")
    return v


def _normalize_target_value(v, raw: str) -> str:
    """Сырой ввод → чистое значение цели через нормализатор канала. 422 при ошибке."""
    raw = raw.strip()
    if not raw:
        raise HTTPException(status_code=422, detail="Укажите значение цели")
    try:
        return v.target_normalizer(raw) if v.target_normalizer else raw
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _target_repo(session, profile_id: int) -> ChannelTargetRepository:
    return ChannelTargetRepository(session, vault=get_vault(), profile_id=profile_id)


def _target_out(trepo: ChannelTargetRepository, channel: str, target) -> ChannelTargetOut:
    """Сериализовать цель, показывая значение нормализованным (как get_target)."""
    return ChannelTargetOut(
        id=target.id,
        name=target.name,
        value=normalize_target(channel, trepo.decrypt_value(target)),
    )


@router.get(
    "/api/profiles/{profile_id}/channels/{channel}/targets",
    response_model=list[ChannelTargetOut],
    dependencies=_PROFILE_GUARD,
)
async def list_targets(
    profile_id: int, channel: str, session: SessionDep
) -> list[ChannelTargetOut]:
    """Список целей канала. Пустой список лениво засевается из legacy-одиночной цели."""
    _require_target_channel(channel)
    trepo = _target_repo(session, profile_id)
    return [_target_out(trepo, channel, t) for t in await trepo.list_targets(channel)]


@router.post(
    "/api/profiles/{profile_id}/channels/{channel}/targets",
    response_model=ChannelTargetOut,
    dependencies=_PROFILE_GUARD,
)
async def create_target(
    profile_id: int, channel: str, body: TargetCreateBody, repo: RepoDep, session: SessionDep
) -> ChannelTargetOut:
    if await repo.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    v = _require_target_channel(channel)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Укажите название цели")
    value = _normalize_target_value(v, body.value)
    trepo = _target_repo(session, profile_id)
    target = await trepo.add_target(channel, name, value)
    return _target_out(trepo, channel, target)


@router.patch(
    "/api/profiles/{profile_id}/channels/{channel}/targets/{target_id}",
    response_model=ChannelTargetOut,
    dependencies=_PROFILE_GUARD,
)
async def patch_target(
    profile_id: int,
    channel: str,
    target_id: int,
    body: TargetPatchBody,
    session: SessionDep,
) -> ChannelTargetOut:
    """Переименовать и/или сменить значение цели. Изоляция по profile_id."""
    v = _require_target_channel(channel)
    trepo = _target_repo(session, profile_id)
    if await trepo.get_target(target_id) is None:
        raise HTTPException(status_code=404, detail="Цель не найдена")
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Название цели не может быть пустым")
        await trepo.rename_target(target_id, name)
    if body.value is not None:
        await trepo.set_value(target_id, _normalize_target_value(v, body.value))
    target = await trepo.get_target(target_id)
    assert target is not None
    return _target_out(trepo, channel, target)


@router.delete(
    "/api/profiles/{profile_id}/channels/{channel}/targets/{target_id}",
    dependencies=_PROFILE_GUARD,
)
async def delete_target(profile_id: int, channel: str, target_id: int, session: SessionDep) -> dict:
    _require_target_channel(channel)
    trepo = _target_repo(session, profile_id)
    deleted = await trepo.delete_target(target_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Цель не найдена")
    return {"deleted": True}


@router.post(
    "/api/profiles/{profile_id}/channels/{channel}/connect",
    response_model=ValidationResult,
    dependencies=_PROFILE_GUARD,
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
    "/api/profiles/{profile_id}/channels/telegraph/create-account",
    response_model=TelegraphCreateOut,
    dependencies=_PROFILE_GUARD,
)
async def telegraph_create_account(
    profile_id: int,
    body: TelegraphCreateBody,
    repo: RepoDep,
) -> TelegraphCreateOut:
    """Создать аккаунт Telegraph и сохранить токен в vault.

    Клиент токен не видит и не копирует — он сохраняется зашифрованным.
    Токен НИКОГДА не логируется и не возвращается в ответе.
    """
    if await repo.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    short_name = body.short_name.strip()
    author_name = body.author_name.strip()
    author_url = body.author_url.strip()

    if not short_name:
        raise HTTPException(status_code=422, detail="short_name обязателен")
    if not author_name:
        raise HTTPException(status_code=422, detail="author_name обязателен")

    params: dict[str, str] = {"short_name": short_name, "author_name": author_name}
    if author_url:
        params["author_url"] = author_url

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post("https://api.telegra.ph/createAccount", data=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Telegraph API недоступен: {exc}") from exc

    if not data.get("ok"):
        err = data.get("error", "неизвестная ошибка")
        raise HTTPException(status_code=422, detail=f"Telegraph: {err}")

    result = data["result"]
    token: str = result["access_token"]  # остаётся только в памяти, не логируется
    await repo.set_credential(profile_id, "telegraph", CredentialKind.API_TOKEN, token)
    await validate_connection(repo, profile_id, "telegraph")

    return TelegraphCreateOut(
        author_name=result.get("author_name", author_name),
        author_url=result.get("author_url", author_url),
    )


@router.post(
    "/api/profiles/{profile_id}/channels/{channel}/validate",
    response_model=ValidationResult,
    dependencies=_PROFILE_GUARD,
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
    msg = "Соединение активно" if state == ConnectionState.LIVE else "Требуется повторный вход"
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
    dependencies=_PROFILE_GUARD,
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
    dependencies=_PROFILE_GUARD,
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
    dependencies=_PROFILE_GUARD,
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
    dependencies=_PROFILE_GUARD,
)
async def browser_login_cancel(profile_id: int, channel: str, repo: RepoDep) -> ValidationResult:
    """Закрыть окно входа, ничего не сохранять."""
    _require_browser_channel(await repo.get_profile(profile_id), channel)

    from crosspost.channels import browser_login as bl  # noqa: PLC0415

    await bl.cancel(profile_id, channel)
    return ValidationResult(state="cancelled", message="Вход отменён.")
