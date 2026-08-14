"""Роут публикации и планирования. Итерация 2а.

Эндпоинты (всё под profile_id — изоляция):
  GET  .../publish/targets?content_type=post   — какие каналы доступны (live+capability)
  POST .../publish                              — опубликовать сейчас (поканальные итоги)
  GET  .../publish/{publication_id}             — поканальные статусы (поллинг)
  POST .../publish/{publication_id}/retry/{ch}  — повторить ОДИН канал
  POST .../scheduled                            — запланировать (сохранить, не исполнять)
  GET  .../scheduled                            — список запланированных
  DELETE .../scheduled/{id}                     — отменить запланированный
  POST   .../scheduled/{id}/publish-now         — опубликовать немедленно

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

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from crosspost.channels.validators import VALIDATORS
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.content.capabilities import supports
from crosspost.content.validation import ContentValidationError, validate
from crosspost.db.log_repo import LogRepository
from crosspost.db.models import ConnectionState, PublicationStatus
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.target_repo import ChannelTargetRepository
from crosspost.db.vault import get_vault
from crosspost.orchestrator.adapter_factory import build_profile_adapter
from crosspost.orchestrator.publish_service import (
    PublishService,
    Selection,
    selections_from_stored,
)
from crosspost.orchestrator.task import new_publication_id
from crosspost.web import media as media_store
from crosspost.web.deps import SessionDep, require_profile_access

logger = logging.getLogger(__name__)
# Все роуты под /api/profiles/{profile_id}/… → изоляция гейтом на весь роутер:
# client не дёрнет чужой profile_id (403), даже мимо UI.
router = APIRouter(tags=["publish"], dependencies=[Depends(require_profile_access)])

_SUCCESS = {PublicationStatus.DONE.value, PublicationStatus.SUBMITTED.value}


@dataclass
class _Run:
    """Снимок публикации для ретрая (в памяти; медиа — на диске).

    content — ИСХОДНЫЙ пост (короткий текст без ссылки): подстановку url статьи
    делает run_binding при ретрае, поэтому храним «сырой» текст, а не рендер.
    """

    content: CanonicalContent
    selections: list[Selection]  # единицы (канал, цель) публикации — для ретрая
    media_key: str
    article_text: str | None = None  # задан → ретрай канала идёт через связку статьи
    article_title: str | None = None
    link_phrase: str | None = None  # фраза ссылки (NULL → «Подробнее»)


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
    screenshot_path: str | None = None
    # Временная ссылка «открыть канал» — приходит из ChannelResult, в БД НЕ пишется.
    # Поэтому есть в ответе publish/retry (из адаптера), но не в поллинге из БД.
    url: str | None = None
    # Мультицели: id цели ("" — таргетлесс) и её имя для показа («Канал · Имя»).
    target: str = ""
    target_name: str = ""


class PublishResult(BaseModel):
    publication_id: str
    outcomes: list[OutcomeOut]


class ScheduledOut(BaseModel):
    id: int
    content_type: str
    text: str
    title: str | None
    # Вербатим-снимок выбора: старый list[str] ИЛИ новый list[{channel,target_id}].
    # Round-trip формы сохраняется; нормализацию в единицы делает executor.
    channels: list[str | dict]
    scheduled_at: str
    status: str
    has_article: bool = False  # задан article_text → карточка покажет бейдж «+ статья»


class ScheduledReportOut(BaseModel):
    """Итог поста, исполненного планировщиком (для «Журнала планировщика»).

    Читается из УЖЕ сохранённого состояния (scheduled_posts + publications),
    поэтому переживает перезагрузку страницы. overall — свёртка поканальных
    статусов: done (все успешны) / partial (часть упала) / failed (все упали).
    cursor — маркер published_at (ISO): фронт хранит его для since/before-догрузки.
    Скриншоты сбоя здесь НЕ отдаём (тяжело) — их тянет отдельный эндпоинт при
    раскрытии карточки.
    """

    id: int
    content_type: str
    text: str
    scheduled_at: str
    cursor: str
    overall: str
    outcomes: list[OutcomeOut]


# ── Хелперы ───────────────────────────────────────────────────────────────────


def _repos(session, profile_id: int) -> tuple[ProfileRepository, PublicationRepository]:
    return (
        ProfileRepository(session, vault=get_vault()),
        PublicationRepository(session, profile_id=profile_id),
    )


async def _require_profile(profile_repo: ProfileRepository, profile_id: int) -> None:
    if await profile_repo.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")


def _make_service(
    profile_repo: ProfileRepository, pub_repo: PublicationRepository, profile_id: int
):
    async def factory(channel: str, target_id: str = ""):
        return await build_profile_adapter(profile_repo, profile_id, channel, target_id)

    return PublishService(pub_repo, factory)


def _parse_selections(channels_form: str) -> list[Selection]:
    """Разобрать поле channels: JSON-массив строк (legacy) или {channel,target_id}."""
    try:
        raw = json.loads(channels_form)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="channels: ожидается JSON-массив") from exc
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=422, detail="Выберите хотя бы один канал")
    try:
        return selections_from_stored(raw)
    except (TypeError, KeyError) as exc:
        raise HTTPException(status_code=422, detail="channels: некорректный элемент") from exc


async def _target_names(session, profile_id: int, target_ids) -> dict[str, str]:
    """{target_id: name} для непустых id — подпись «Канал · Имя» в итогах."""
    trepo = ChannelTargetRepository(session, vault=get_vault(), profile_id=profile_id)
    names: dict[str, str] = {}
    for tid in {t for t in target_ids if t}:
        try:
            tgt = await trepo.get_target(int(tid))
        except (ValueError, TypeError):
            tgt = None
        names[tid] = tgt.name if tgt is not None else ""
    return names


def _outcome_out(outcome, names: dict[str, str]) -> OutcomeOut:
    """ChannelOutcome → OutcomeOut, доклеивая имя цели по её id."""
    data = dict(outcome.__dict__)
    tid = data.get("target", "") or ""
    return OutcomeOut(**data, target_name=names.get(tid, ""))


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
    trepo = ChannelTargetRepository(session, vault=get_vault(), profile_id=profile_id)
    out: list[TargetOut] = []
    for ch, v in VALIDATORS.items():
        if not v.enabled:
            continue
        state = conns.get(ch)
        state_str = str(state) if state is not None else "not_connected"
        supported = supports(ch, ctype)
        live = state == ConnectionState.LIVE
        has_target = True
        if v.needs_target:
            # «Есть цель?» = есть хотя бы одна ChannelTarget (список лениво сидится
            # из legacy: CredentialKind.TARGET у браузерных, target_channel-блоб у TG).
            has_target = bool(await trepo.list_targets(ch))
        eligible = live and supported and has_target
        if not supported:
            reason = f"не поддерживает {ctype.value}"
        elif not live:
            reason = "переподключите в настройках" if state else "не подключён"
        elif not has_target:
            reason = f"укажите «{v.target_label.lower()}» в настройках"
        else:
            reason = ""
        out.append(
            TargetOut(
                channel=ch,
                title=v.title or ch,
                kind=v.kind,
                state=state_str,
                eligible=eligible,
                reason=reason,
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
    article_text: str = Form(""),  # тело лонгрида → статья Telegraph + ссылка в посте
    link_phrase: str = Form(""),  # фраза ссылки на статью (пусто → «Подробнее»)
    media: list[UploadFile] = File(default=[]),  # noqa: B008
) -> PublishResult:
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    selections = _parse_selections(channels)
    # Отсекаем нерабочие каналы/цели ДО отправки (defense in depth к фронту).
    await _reject_ineligible(profile_repo, profile_id, selections, content_type, session=session)

    publication_id = new_publication_id()
    media_paths = await _save_media(media, publication_id)
    content = _build_content(content_type, text, title, media_paths)
    _validate_or_422(content)

    svc = _make_service(profile_repo, pub_repo, profile_id)
    outcomes = await svc.publish(
        content,
        selections,
        publication_id=publication_id,
        article_text=article_text or None,
        article_title=title or None,
        link_phrase=link_phrase or None,
    )

    _RUNS[(profile_id, publication_id)] = _Run(
        content,
        selections,
        publication_id,
        article_text=article_text or None,
        article_title=title or None,
        link_phrase=link_phrase or None,
    )
    _cleanup_if_final(profile_id, publication_id, outcomes)
    names = await _target_names(session, profile_id, [o.target for o in outcomes])
    return PublishResult(
        publication_id=publication_id,
        outcomes=[_outcome_out(o, names) for o in outcomes],
    )


@router.get("/api/profiles/{profile_id}/publish/{publication_id}", response_model=list[OutcomeOut])
async def publish_status(
    profile_id: int, publication_id: str, session: SessionDep
) -> list[OutcomeOut]:
    _, pub_repo = _repos(session, profile_id)
    rows = await pub_repo.list_statuses(publication_id)
    names = await _target_names(session, profile_id, [r.target for r in rows])
    return [
        OutcomeOut(
            channel=r.channel,
            status=str(r.status),
            external_id=r.external_id,
            error=r.error,
            target=r.target,
            target_name=names.get(r.target, ""),
        )
        for r in rows
    ]


@router.post(
    "/api/profiles/{profile_id}/publish/{publication_id}/retry/{channel}",
    response_model=OutcomeOut,
)
async def publish_retry(
    profile_id: int, publication_id: str, channel: str, session: SessionDep, target_id: str = ""
) -> OutcomeOut:
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    run = _RUNS.get((profile_id, publication_id))
    if run is None:
        raise HTTPException(status_code=409, detail="Публикация не найдена для повтора")
    sel = Selection(channel, target_id)
    if sel not in run.selections:
        raise HTTPException(status_code=404, detail="Цель не входит в публикацию")

    svc = _make_service(profile_repo, pub_repo, profile_id)
    if run.article_text:
        # Ретрай канала при связке со статьёй идёт через тот же секвенсор:
        # статья идемпотентно переиспользуется (url подставится), fail-closed сохраняется.
        # Не ретраим сам telegraph поштучно — он часть фазы статьи.
        outcomes = await svc.publish(
            run.content,
            [sel],
            publication_id=publication_id,
            article_text=run.article_text,
            article_title=run.article_title,
            link_phrase=run.link_phrase,
        )
        outcome = next((o for o in outcomes if o.channel == channel), outcomes[-1])
    else:
        outcome = await svc.retry_channel(run.content, sel, publication_id=publication_id)

    all_now = await pub_repo.list_statuses(publication_id)
    if all(str(s.status) in _SUCCESS for s in all_now):
        media_store.cleanup_media(run.media_key)
        _RUNS.pop((profile_id, publication_id), None)
    names = await _target_names(session, profile_id, [outcome.target])
    return _outcome_out(outcome, names)


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
    article_text: str = Form(""),  # тело лонгрида → статья Telegraph при исполнении
    link_phrase: str = Form(""),  # фраза ссылки на статью (пусто → «Подробнее»)
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
    if not isinstance(channel_list, list) or not channel_list:
        raise HTTPException(status_code=422, detail="Выберите хотя бы один канал")

    # Валидируем по единицам, но храним channel_list ВЕРБАТИМ (старый list[str]
    # или новый list[{channel,target_id}]) — executor нормализует при исполнении,
    # round-trip формы сохраняется (см. selections_from_stored).
    try:
        selections = selections_from_stored(channel_list)
    except (TypeError, KeyError) as exc:
        raise HTTPException(status_code=422, detail="channels: некорректный элемент") from exc
    await _reject_ineligible(profile_repo, profile_id, selections, content_type, session=session)

    key = f"sched-{new_publication_id()}"
    media_paths = await _save_media(media, key)
    content = _build_content(content_type, text, title, media_paths)
    _validate_or_422(content)

    post = await pub_repo.create_scheduled(
        content_type=content_type,
        text=text,
        title=title or None,
        media_paths=media_paths,
        channels=channel_list,
        scheduled_at=when,
        article_text=article_text or None,
        link_phrase=link_phrase or None,
    )
    return _scheduled_out(post)


@router.get("/api/profiles/{profile_id}/scheduled", response_model=list[ScheduledOut])
async def list_scheduled(profile_id: int, session: SessionDep) -> list[ScheduledOut]:
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)
    return [_scheduled_out(p) for p in await pub_repo.list_scheduled()]


@router.get(
    "/api/profiles/{profile_id}/scheduled/report",
    response_model=list[ScheduledReportOut],
)
async def scheduled_report(
    profile_id: int,
    session: SessionDep,
    limit: int = 20,
    before: str | None = None,
    since: str | None = None,
) -> list[ScheduledReportOut]:
    """Журнал планировщика: исполненные посты с поканальными итогами (без скриншотов).

    Читаем ТОЛЬКО существующее состояние — новых записей в БД не создаём:
      scheduled_posts (status=published) → publications (schedpost-<id>).
    Пагинация/инкремент по published_at (см. list_executed):
      • since=<iso> — только исполненные позже метки (догрузка новых, [] если нет);
      • before=<iso> — страница «старше метки» («показать ещё»);
      • limit — размер страницы (по умолчанию 20, потолок 100).
    Скриншоты сбоя не в этом ответе — их подтягивает .../scheduled/{id}/screenshots
    при раскрытии карточки. Отчёт переживает перезагрузку; фронт догружает без refresh.
    """
    # Локальный импорт: executor тянет crosspost.web.media — избегаем цикла загрузки.
    from crosspost.scheduler.executor import scheduled_publication_id

    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    limit = max(1, min(limit, 100))
    before_dt = _parse_marker(before)
    since_dt = _parse_marker(since)

    reports: list[ScheduledReportOut] = []
    for post in await pub_repo.list_executed(limit=limit, before=before_dt, since=since_dt):
        pubid = scheduled_publication_id(post.id)
        rows = await pub_repo.list_statuses(pubid)
        names = await _target_names(session, profile_id, [r.target for r in rows])
        outcomes = [
            OutcomeOut(
                channel=r.channel,
                status=str(r.status),
                external_id=r.external_id,
                error=r.error,
                target=r.target,
                target_name=names.get(r.target, ""),
            )
            for r in rows
        ]
        assert post.published_at is not None  # list_executed фильтрует published_at IS NOT NULL
        reports.append(
            ScheduledReportOut(
                id=post.id,
                content_type=post.content_type,
                text=post.text,
                scheduled_at=post.scheduled_at.isoformat(),
                cursor=post.published_at.isoformat(),
                overall=_overall_status(outcomes),
                outcomes=outcomes,
            )
        )
    return reports


@router.get("/api/profiles/{profile_id}/scheduled/{scheduled_id}/screenshots")
async def scheduled_screenshots(
    profile_id: int, scheduled_id: int, session: SessionDep
) -> dict[str, str]:
    """Скриншоты СБОЯ по каналам исполненного поста (подгружаются при раскрытии карточки).

    Тяжёлый джойн по логам вынесен сюда, чтобы не тащить пути в общий ответ журнала.
    Скриншот — артефакт сбоя: отдаём только для каналов с НЕуспешным финальным
    статусом (канал мог упасть на попытке 1 и дожаться на ретрае — старый скриншот
    к успеху не относится). Ключ — канал, значение — путь к скриншоту.
    """
    from crosspost.scheduler.executor import scheduled_publication_id

    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    pubid = scheduled_publication_id(scheduled_id)
    failed = {
        r.channel for r in await pub_repo.list_statuses(pubid) if str(r.status) not in _SUCCESS
    }
    if not failed:
        return {}

    shots: dict[str, str] = {}
    for lg in await LogRepository(session).list_for_profile(profile_id):
        if (
            lg.screenshot_path
            and lg.publication_id == pubid
            and lg.channel in failed
            and lg.channel not in shots  # логи новыми сверху → первый = самый свежий
        ):
            shots[lg.channel] = lg.screenshot_path
    return shots


class RescheduleBody(BaseModel):
    scheduled_at: str  # ISO-8601


@router.patch("/api/profiles/{profile_id}/scheduled/{scheduled_id}", response_model=ScheduledOut)
async def reschedule(
    profile_id: int, scheduled_id: int, body: RescheduleBody, session: SessionDep
) -> ScheduledOut:
    """Сдвинуть время запланированного поста (правка из UI). Изоляция по profile_id."""
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)
    try:
        when = datetime.fromisoformat(body.scheduled_at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="scheduled_at: ISO-8601 дата/время") from exc
    if not await pub_repo.update_scheduled_time(scheduled_id, when):
        raise HTTPException(status_code=404, detail="Запланированный пост не найден")
    post = await pub_repo.get_scheduled(scheduled_id)
    assert post is not None
    return _scheduled_out(post)


@router.delete("/api/profiles/{profile_id}/scheduled/{scheduled_id}")
async def cancel_scheduled(profile_id: int, scheduled_id: int, session: SessionDep) -> dict:
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


@router.post("/api/profiles/{profile_id}/scheduled/{scheduled_id}/publish-now")
async def publish_scheduled_now(
    profile_id: int, scheduled_id: int, request: Request, session: SessionDep
) -> dict:
    """Немедленно опубликовать запланированный пост, не дожидаясь тика исполнителя.

    Возвращает поканальные итоги: status (done/failed/needs_relogin), error.
    """
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    post = await pub_repo.get_scheduled(scheduled_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Запланированный пост не найден")

    executor = getattr(request.app.state, "executor", None)
    if executor is None:
        raise HTTPException(
            status_code=503, detail="Исполнитель расписания недоступен (SCHEDULER_ENABLED=false)"
        )

    await executor.publish_post_now(scheduled_id)

    # Читаем итоги из БД (executor пишет в свои сессии, коммитит до return)
    from crosspost.scheduler.executor import scheduled_publication_id

    pubid = scheduled_publication_id(scheduled_id)
    statuses = await pub_repo.list_statuses(pubid)
    results = [
        {"channel": s.channel, "status": str(s.status), "error": s.error or None} for s in statuses
    ]
    all_done = all(str(s.status) in _SUCCESS for s in statuses) if statuses else False
    return {"triggered": True, "results": results, "all_done": all_done}


# ── Внутреннее ────────────────────────────────────────────────────────────────


async def _reject_ineligible(
    profile_repo: ProfileRepository,
    profile_id: int,
    selections: list[Selection],
    content_type: str,
    *,
    session,
) -> None:
    """Отклонить, если среди выбранных единиц есть неlive/неподдерживающий/без цели.

    Проверка на уровне ЦЕЛИ (мультицели): канал должен быть enabled, live и
    поддерживать тип; а для needs_target-канала выбранная цель должна существовать
    (по target_id в channel_targets) — либо, для legacy-выбора без target_id,
    должна быть заведена одиночная цель (back-compat через CredentialKind.TARGET).
    """
    try:
        ctype = ContentType(content_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Неизвестный тип: {content_type}") from exc
    conns = {c.channel: c.state for c in await profile_repo.get_connections(profile_id)}
    trepo = ChannelTargetRepository(session, vault=get_vault(), profile_id=profile_id)
    for sel in selections:
        ch = sel.channel
        v = VALIDATORS.get(ch)
        if v is None or not v.enabled:
            raise HTTPException(status_code=422, detail=f"Канал '{ch}' недоступен")
        if not supports(ch, ctype):
            raise HTTPException(status_code=422, detail=f"'{ch}' не поддерживает {ctype.value}")
        if conns.get(ch) != ConnectionState.LIVE:
            raise HTTPException(
                status_code=422, detail=f"'{ch}' не подключён — переподключите в настройках"
            )
        if not v.needs_target:
            continue
        if sel.target_id:
            try:
                target = await trepo.get_target(int(sel.target_id))
            except (ValueError, TypeError):
                target = None
            if target is None or target.channel != ch:
                raise HTTPException(status_code=422, detail=f"'{ch}': выбранная цель не найдена")
        elif not await trepo.list_targets(ch):
            # legacy-выбор без target_id: хотя бы одна цель должна существовать (список
            # лениво сидится из старой одиночной цели / telegram-блоба).
            raise HTTPException(
                status_code=422,
                detail=f"'{ch}': укажите «{v.target_label.lower()}» в настройках",
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


def _parse_marker(value: str | None) -> datetime | None:
    """Разобрать курсор журнала (published_at ISO). Кривой/пустой → None (без фильтра)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="cursor: ожидается ISO-8601") from exc


def _overall_status(outcomes: list[OutcomeOut]) -> str:
    """Свёртка поканальных статусов в общий вердикт для карточки журнала.

    done — все каналы успешны (done/submitted); failed — все упали; partial —
    часть упала. Пусто (нет строк) — unknown (пост помечен published без итогов).
    """
    if not outcomes:
        return "unknown"
    ok = sum(1 for o in outcomes if o.status in _SUCCESS)
    if ok == len(outcomes):
        return "done"
    if ok == 0:
        return "failed"
    return "partial"


def _scheduled_out(post) -> ScheduledOut:
    return ScheduledOut(
        id=post.id,
        content_type=post.content_type,
        text=post.text,
        title=post.title,
        channels=list(post.channels or []),
        scheduled_at=post.scheduled_at.isoformat(),
        status=str(post.status),
        has_article=bool(post.article_text),
    )
