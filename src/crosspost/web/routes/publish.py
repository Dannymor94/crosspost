"""Роут публикации и планирования. Итерация 2а.

Эндпоинты (всё под profile_id — изоляция):
  GET  .../publish/targets?content_type=post   — какие каналы доступны (live+capability)
  POST .../publish                              — опубликовать сейчас (поканальные итоги)
  GET  .../publish/{publication_id}             — поканальные статусы (поллинг)
  POST .../publish/{publication_id}/retry/{ch}  — повторить ОДИН канал
  POST .../scheduled                            — запланировать (сохранить, не исполнять)
  GET  .../scheduled                            — список запланированных
  DELETE .../scheduled/{id}                     — отменить запланированный

Публикация синхронная: запрос ждёт поканальные итоги (частичный успех — норма).
Медиа во временном хранилище; чистим, когда все каналы финализировались успешно.
Контент publication'а держим в памяти (_RUNS) для ретрая без повторной загрузки.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from crosspost.channels.validators import VALIDATORS
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.content.capabilities import supports
from crosspost.content.validation import ContentValidationError, validate
from crosspost.db.models import ConnectionState, PublicationStatus
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.vault import get_vault
from crosspost.orchestrator.adapter_factory import build_profile_adapter
from crosspost.orchestrator.publish_service import PublishService
from crosspost.orchestrator.task import new_publication_id
from crosspost.web import media as media_store
from crosspost.web.deps import SessionDep

logger = logging.getLogger(__name__)
router = APIRouter(tags=["publish"])

_SUCCESS = {PublicationStatus.DONE.value, PublicationStatus.SUBMITTED.value}


@dataclass
class _Run:
    """Снимок публикации для ретрая (в памяти; медиа — на диске)."""

    content: CanonicalContent
    channels: list[str]
    media_key: str


_RUNS: dict[tuple[int, str], _Run] = {}


# ── Схемы ─────────────────────────────────────────────────────────────────────


class TargetOut(BaseModel):
    channel: str
    title: str
    kind: str
    state: str
    eligible: bool
    reason: str = ""


class OutcomeOut(BaseModel):
    channel: str
    status: str
    external_id: str | None = None
    error: str | None = None


class PublishResult(BaseModel):
    publication_id: str
    outcomes: list[OutcomeOut]


class ScheduledOut(BaseModel):
    id: int
    content_type: str
    text: str
    title: str | None
    channels: list[str]
    scheduled_at: str
    status: str


# ── Хелперы ───────────────────────────────────────────────────────────────────


def _repos(session, profile_id: int) -> tuple[ProfileRepository, PublicationRepository]:
    return (
        ProfileRepository(session, vault=get_vault()),
        PublicationRepository(session, profile_id=profile_id),
    )


async def _require_profile(profile_repo: ProfileRepository, profile_id: int) -> None:
    if await profile_repo.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")


def _make_service(profile_repo: ProfileRepository, pub_repo: PublicationRepository, profile_id: int):
    async def factory(channel: str):
        return await build_profile_adapter(profile_repo, profile_id, channel)

    return PublishService(pub_repo, factory)


def _build_content(content_type: str, text: str, title: str | None, media_paths: list[str]):
    try:
        ctype = ContentType(content_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Неизвестный тип: {content_type}") from exc
    return CanonicalContent(
        type=ctype, text=text, title=title or None, media_paths=[Path(p) for p in media_paths]
    )


# ── Targets (доступные каналы) ────────────────────────────────────────────────


@router.get("/api/profiles/{profile_id}/publish/targets", response_model=list[TargetOut])
async def publish_targets(
    profile_id: int, session: SessionDep, content_type: str = "post"
) -> list[TargetOut]:
    profile_repo, _ = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)
    try:
        ctype = ContentType(content_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Неизвестный тип: {content_type}") from exc

    conns = {c.channel: c.state for c in await profile_repo.get_connections(profile_id)}
    out: list[TargetOut] = []
    for ch, v in VALIDATORS.items():
        if not v.enabled:
            continue
        state = conns.get(ch)
        state_str = str(state) if state is not None else "not_connected"
        supported = supports(ch, ctype)
        live = state == ConnectionState.LIVE
        eligible = live and supported
        if not supported:
            reason = f"не поддерживает {ctype.value}"
        elif not live:
            reason = "переподключите в настройках" if state else "не подключён"
        else:
            reason = ""
        out.append(
            TargetOut(
                channel=ch, title=v.title or ch, kind=v.kind,
                state=state_str, eligible=eligible, reason=reason,
            )
        )
    return out


# ── Publish now ───────────────────────────────────────────────────────────────


@router.post("/api/profiles/{profile_id}/publish", response_model=PublishResult)
async def publish_now(
    profile_id: int,
    session: SessionDep,
    content_type: str = Form("post"),
    text: str = Form(""),
    title: str | None = Form(None),
    channels: str = Form(...),  # JSON-массив каналов
    media: list[UploadFile] = File(default=[]),  # noqa: B008
) -> PublishResult:
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    try:
        channel_list = json.loads(channels)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="channels: ожидается JSON-массив") from exc
    if not channel_list:
        raise HTTPException(status_code=422, detail="Выберите хотя бы один канал")

    # Отсекаем нерабочие каналы ДО отправки (defense in depth к фронту).
    await _reject_ineligible(profile_repo, profile_id, channel_list, content_type)

    publication_id = new_publication_id()
    media_paths = await _save_media(media, publication_id)
    content = _build_content(content_type, text, title, media_paths)
    _validate_or_422(content)

    svc = _make_service(profile_repo, pub_repo, profile_id)
    outcomes = await svc.publish(content, channel_list, publication_id=publication_id)

    _RUNS[(profile_id, publication_id)] = _Run(content, channel_list, publication_id)
    _cleanup_if_final(profile_id, publication_id, outcomes)
    return PublishResult(
        publication_id=publication_id,
        outcomes=[OutcomeOut(**o.__dict__) for o in outcomes],
    )


@router.get(
    "/api/profiles/{profile_id}/publish/{publication_id}", response_model=list[OutcomeOut]
)
async def publish_status(
    profile_id: int, publication_id: str, session: SessionDep
) -> list[OutcomeOut]:
    _, pub_repo = _repos(session, profile_id)
    rows = await pub_repo.list_statuses(publication_id)
    return [
        OutcomeOut(channel=r.channel, status=str(r.status), external_id=r.external_id, error=r.error)
        for r in rows
    ]


@router.post(
    "/api/profiles/{profile_id}/publish/{publication_id}/retry/{channel}",
    response_model=OutcomeOut,
)
async def publish_retry(
    profile_id: int, publication_id: str, channel: str, session: SessionDep
) -> OutcomeOut:
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    run = _RUNS.get((profile_id, publication_id))
    if run is None:
        raise HTTPException(status_code=409, detail="Публикация не найдена для повтора")
    if channel not in run.channels:
        raise HTTPException(status_code=404, detail="Канал не входит в публикацию")

    svc = _make_service(profile_repo, pub_repo, profile_id)
    outcome = await svc.retry_channel(run.content, channel, publication_id=publication_id)

    all_now = await pub_repo.list_statuses(publication_id)
    if all(str(s.status) in _SUCCESS for s in all_now):
        media_store.cleanup_media(run.media_key)
        _RUNS.pop((profile_id, publication_id), None)
    return OutcomeOut(**outcome.__dict__)


# ── Scheduled ─────────────────────────────────────────────────────────────────


@router.post("/api/profiles/{profile_id}/scheduled", response_model=ScheduledOut)
async def schedule_post(
    profile_id: int,
    session: SessionDep,
    scheduled_at: str = Form(...),
    content_type: str = Form("post"),
    text: str = Form(""),
    title: str | None = Form(None),
    channels: str = Form(...),
    media: list[UploadFile] = File(default=[]),  # noqa: B008
) -> ScheduledOut:
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    try:
        when = datetime.fromisoformat(scheduled_at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="scheduled_at: ISO-8601 дата/время") from exc
    try:
        channel_list = json.loads(channels)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="channels: ожидается JSON-массив") from exc
    if not channel_list:
        raise HTTPException(status_code=422, detail="Выберите хотя бы один канал")

    await _reject_ineligible(profile_repo, profile_id, channel_list, content_type)

    key = f"sched-{new_publication_id()}"
    media_paths = await _save_media(media, key)
    content = _build_content(content_type, text, title, media_paths)
    _validate_or_422(content)

    post = await pub_repo.create_scheduled(
        content_type=content_type, text=text, title=title or None,
        media_paths=media_paths, channels=channel_list, scheduled_at=when,
    )
    return _scheduled_out(post)


@router.get("/api/profiles/{profile_id}/scheduled", response_model=list[ScheduledOut])
async def list_scheduled(profile_id: int, session: SessionDep) -> list[ScheduledOut]:
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)
    return [_scheduled_out(p) for p in await pub_repo.list_scheduled()]


@router.delete("/api/profiles/{profile_id}/scheduled/{scheduled_id}")
async def cancel_scheduled(
    profile_id: int, scheduled_id: int, session: SessionDep
) -> dict:
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    post = await pub_repo.get_scheduled(scheduled_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Запланированный пост не найден")
    media_paths = list(post.media_paths or [])
    await pub_repo.cancel_scheduled(scheduled_id)
    # Чистим медиа отменённого поста (каталог = родитель первого файла).
    if media_paths:
        parent = Path(media_paths[0]).parent
        media_store.cleanup_media(parent.name)
    return {"cancelled": True}


# ── Внутреннее ────────────────────────────────────────────────────────────────


async def _reject_ineligible(
    profile_repo: ProfileRepository, profile_id: int, channels: list[str], content_type: str
) -> None:
    """Отклонить, если среди выбранных есть неlive или неподдерживающий тип канал."""
    try:
        ctype = ContentType(content_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Неизвестный тип: {content_type}") from exc
    conns = {c.channel: c.state for c in await profile_repo.get_connections(profile_id)}
    for ch in channels:
        v = VALIDATORS.get(ch)
        if v is None or not v.enabled:
            raise HTTPException(status_code=422, detail=f"Канал '{ch}' недоступен")
        if not supports(ch, ctype):
            raise HTTPException(
                status_code=422, detail=f"'{ch}' не поддерживает {ctype.value}"
            )
        if conns.get(ch) != ConnectionState.LIVE:
            raise HTTPException(
                status_code=422, detail=f"'{ch}' не подключён — переподключите в настройках"
            )


def _validate_or_422(content: CanonicalContent) -> None:
    try:
        validate(content)
    except ContentValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _save_media(files, key: str) -> list[str]:
    real = [f for f in (files or []) if getattr(f, "filename", None)]
    if not real:
        return []
    return await media_store.save_uploads(real, key)


def _cleanup_if_final(profile_id: int, publication_id: str, outcomes) -> None:
    """Если все каналы завершились успешно — чистим медиа и снимок (ретрай не нужен)."""
    if all(o.status in _SUCCESS for o in outcomes):
        media_store.cleanup_media(publication_id)
        _RUNS.pop((profile_id, publication_id), None)


def _scheduled_out(post) -> ScheduledOut:
    return ScheduledOut(
        id=post.id,
        content_type=post.content_type,
        text=post.text,
        title=post.title,
        channels=list(post.channels or []),
        scheduled_at=post.scheduled_at.isoformat(),
        status=str(post.status),
    )
