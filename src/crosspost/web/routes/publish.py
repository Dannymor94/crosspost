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

import base64
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from crosspost.channels.validators import PLATFORM_ORDER, VALIDATORS
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.content.capabilities import supports, video_ineligible_reason
from crosspost.content.media_kind import MediaKind, detect_media_kind
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
from crosspost.scheduler.media_sources import MediaSourceError, fetch_media
from crosspost.web import media as media_store
from crosspost.web.deps import AdminUser, CurrentUser, SessionDep, require_profile_access
from crosspost.web.signed_media import redact_media_tokens

logger = logging.getLogger(__name__)
_PLATFORM_RANK: dict[str, int] = {p: i for i, p in enumerate(PLATFORM_ORDER)}
# Alle routes under /api/profiles/{profile_id}/… — isolation gate covers entire router.
router = APIRouter(tags=["publish"], dependencies=[Depends(require_profile_access)])


def _make_http_client() -> httpx.AsyncClient:
    """Фабрика httpx-клиента — переопределяется в тестах через unittest.mock.patch."""
    return httpx.AsyncClient()


def _make_preview_client() -> httpx.AsyncClient:
    """Preview-specific httpx client factory — patched in tests."""
    return httpx.AsyncClient()


_SUCCESS = {PublicationStatus.DONE.value, PublicationStatus.SUBMITTED.value}

# ── Preview block cache ───────────────────────────────────────────────────────
# Process-level URL → (data_uri, expiry) cache.
# Avoids re-fetching the same image URL on every debounced keystroke while the user
# edits. TTL = 5 minutes; capped at 50 entries (oldest evicted first).
_PREVIEW_CACHE_MAX = 50
_PREVIEW_CACHE_TTL = 300.0  # seconds

# key: url → (data_uri: str, expiry: float)
_preview_url_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()


async def _fetch_preview_data_uri(url: str, *, client: httpx.AsyncClient) -> str:
    """Fetch image URL and return base64 data URI, using the process-level cache.

    Only http(s) URLs are accepted.  parse_blocks guarantees this via _is_sole_url
    (bare filenames become ParagraphBlock, never ImageBlock), but we guard here
    explicitly so a future caller cannot accidentally reach the URL-keyed cache
    with a filesystem path, which would be a cross-profile data leak.

    Caps individual image downloads at 1 MB so preview responses stay small.
    """
    # Defense-in-depth: reject non-http(s) URLs regardless of caller assumptions.
    if not url.startswith(("http://", "https://")):
        raise MediaSourceError(f"preview: только http(s) URL допустимы (получено: {url!r})")

    now = time.monotonic()
    if url in _preview_url_cache:
        data_uri, expiry = _preview_url_cache[url]
        if now < expiry:
            _preview_url_cache.move_to_end(url)
            return data_uri
        del _preview_url_cache[url]

    media = await fetch_media(url, client=client, max_bytes=1024 * 1024)
    ext = media.filename.rsplit(".", 1)[-1].lower() if "." in media.filename else "jpeg"
    mime = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    data_uri = f"data:{mime};base64,{base64.b64encode(media.data).decode()}"

    _preview_url_cache[url] = (data_uri, now + _PREVIEW_CACHE_TTL)
    while len(_preview_url_cache) > _PREVIEW_CACHE_MAX:
        _preview_url_cache.popitem(last=False)
    return data_uri


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
    # "content" = fixable here (wrong media type); "connection" = fixable in settings
    ineligible_kind: str = ""
    # Мультицели: одна запись на КАЖДУЮ цель канала. target_id="" — таргетлесс
    # (telegraph) или канал без заведённых целей (плейсхолдер-ineligible).
    target_id: str = ""
    target_name: str = ""
    platform: str = ""
    platform_title: str = ""
    short_label: str = ""


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
    media_paths: list[str] = []  # пути к файлам медиа (для показа в форме редактирования)
    reject_reason: str | None = None


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


def _target_reason(
    v,
    ctype,
    supported: bool,
    live: bool,
    state,
    *,
    has_target: bool,
    channel: str = "",
    has_video: bool = False,
    video_bytes: int = 0,
) -> str:
    """Причина неготовности единицы публикации (порядок приоритета важен)."""
    if channel:
        vreason = video_ineligible_reason(
            channel, v.max_video_bytes, has_video=has_video, video_bytes=video_bytes
        )
        if vreason:
            return vreason
    if not supported:
        return f"Не поддерживает тип «{ctype.value}»"
    if not live:
        return "Переподключите в настройках" if state else "Не подключён"
    if not has_target:
        return f"Укажите «{v.target_label.lower()}» в настройках"
    return ""


def _upload_video_bytes(files) -> int:
    """Return the total size of uploaded video files in bytes (0 if unknown)."""
    total = 0
    for f in files or []:
        if not getattr(f, "filename", None):
            continue
        try:
            f.file.seek(0, 2)
            total += f.file.tell()
            f.file.seek(0)
        except Exception:  # noqa: BLE001
            pass
    return total


def _has_video(files, image: str = "") -> bool:
    """True if any attached media (uploaded file or URL/filename) is a video.

    Server-side signal for the eligibility gate — не доверяем клиентскому has_video.
    """
    for f in files or []:
        name = getattr(f, "filename", None)
        if name and detect_media_kind(name) == MediaKind.VIDEO:
            return True
    if image and detect_media_kind(image.split("?")[0]) == MediaKind.VIDEO:
        return True
    return False


def _ineligible_kind(eligible: bool, video_ok: bool, supported: bool) -> str:
    """'content' if fixable by changing media/type here; 'connection' if fixable in settings."""
    if eligible:
        return ""
    if not video_ok or not supported:
        return "content"
    return "connection"


def _build_content(content_type: str, text: str, title: str | None, media_paths: list[str]):
    try:
        ctype = ContentType(content_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Неизвестный тип: {content_type}") from exc
    return CanonicalContent(
        type=ctype, text=text, title=title or None, media_paths=[Path(p) for p in media_paths]
    )


# ── Article block preview ─────────────────────────────────────────────────────


class PreviewBlock(BaseModel):
    type: str  # "p" | "img"
    index: int
    text: str | None = None  # paragraph text
    url: str | None = None  # image source URL
    data_uri: str | None = None  # base64 data URI if resolved
    error: str | None = None  # MediaSourceError reason if failed


@router.post("/api/profiles/{profile_id}/publish/resolve-media")
async def resolve_media_preview(
    profile_id: int,
    session: SessionDep,
    image: str = Form(""),
) -> dict:
    """Preview-resolve a media URL (http, Yandex.Disk, source-folder filename).

    Returns {data_uri, filename} for thumbnail display before publishing.
    Raises 422 with the server's reason on SSRF, unreachable, oversized, path-escape.
    Cap: 1 MB (preview only — publish re-fetches at full size).
    """
    if not image.strip():
        raise HTTPException(status_code=422, detail="image: URL не указан")
    from crosspost.db.settings_repo import ProfileSettingsRepository

    folder = await ProfileSettingsRepository(session, profile_id=profile_id).get_media_folder()
    try:
        async with _make_http_client() as http:
            media = await fetch_media(
                image.strip(), folder=folder, client=http, max_bytes=1024 * 1024
            )
    except MediaSourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    ext = media.filename.rsplit(".", 1)[-1].lower() if "." in media.filename else "jpeg"
    mime = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    data_uri = f"data:{mime};base64,{base64.b64encode(media.data).decode()}"
    return {"data_uri": data_uri, "filename": media.filename}


@router.post(
    "/api/profiles/{profile_id}/publish/preview-blocks",
    response_model=list[PreviewBlock],
)
async def preview_article_blocks(
    profile_id: int,
    text: str = Form(""),
) -> list[PreviewBlock]:
    """Parse article text into blocks and attempt image resolution.

    Returns ordered blocks mirroring article_blocks.parse_blocks.
    Image blocks are resolved via fetch_media (SSRF guard, Yandex.Disk, etc.)
    and returned as base64 data URIs (capped at 1 MB per image) so the client
    can render thumbnails without extra requests. Failed blocks carry the reason
    string; they are never silently dropped.

    Caching: resolved images are cached by URL for 5 minutes (_PREVIEW_CACHE_TTL)
    to avoid hammering remote hosts during debounced edits. Cache is process-level,
    capped at _PREVIEW_CACHE_MAX entries (LRU eviction).
    """
    from crosspost.content.article_blocks import ParagraphBlock, parse_blocks

    blocks = parse_blocks(text)
    result: list[PreviewBlock] = []

    async with _make_preview_client() as client:
        for i, block in enumerate(blocks):
            if isinstance(block, ParagraphBlock):
                result.append(PreviewBlock(type="p", index=i, text=block.text))
            else:
                try:
                    data_uri = await _fetch_preview_data_uri(block.url, client=client)
                    result.append(
                        PreviewBlock(type="img", index=i, url=block.url, data_uri=data_uri)
                    )
                except MediaSourceError as exc:
                    result.append(PreviewBlock(type="img", index=i, url=block.url, error=str(exc)))

    return result


# ── Targets (доступные каналы) ────────────────────────────────────────────────


@router.get("/api/profiles/{profile_id}/publish/targets", response_model=list[TargetOut])
async def publish_targets(
    profile_id: int,
    session: SessionDep,
    content_type: str = "post",
    has_video: bool = False,
    video_bytes: int = 0,
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
        video_ok = not video_ineligible_reason(
            ch, v.max_video_bytes, has_video=has_video, video_bytes=video_bytes
        )

        if not v.needs_target:
            # Таргетлесс-канал (telegraph): ровно одна единица, target_id="".
            elig = live and supported and video_ok
            out.append(
                TargetOut(
                    channel=ch,
                    title=v.title or ch,
                    kind=v.kind,
                    state=state_str,
                    eligible=elig,
                    reason=_target_reason(
                        v,
                        ctype,
                        supported,
                        live,
                        state,
                        has_target=True,
                        channel=ch,
                        has_video=has_video,
                        video_bytes=video_bytes,
                    ),
                    ineligible_kind=_ineligible_kind(elig, video_ok, supported),
                    platform=v.platform,
                    platform_title=v.platform_title,
                    short_label=v.short_label,
                )
            )
            continue

        # Мультицели: по записи на каждую цель (список лениво сидится из legacy).
        targets = await trepo.list_targets(ch)
        if not targets:
            # Ни одной цели — плейсхолдер, чтобы канал был виден как недоступный.
            out.append(
                TargetOut(
                    channel=ch,
                    title=v.title or ch,
                    kind=v.kind,
                    state=state_str,
                    eligible=False,
                    reason=_target_reason(
                        v,
                        ctype,
                        supported,
                        live,
                        state,
                        has_target=False,
                        channel=ch,
                        has_video=has_video,
                        video_bytes=video_bytes,
                    ),
                    ineligible_kind=_ineligible_kind(False, video_ok, supported),
                    platform=v.platform,
                    platform_title=v.platform_title,
                    short_label=v.short_label,
                )
            )
            continue
        elig = live and supported and video_ok
        reason = _target_reason(
            v,
            ctype,
            supported,
            live,
            state,
            has_target=True,
            channel=ch,
            has_video=has_video,
            video_bytes=video_bytes,
        )
        ikind = _ineligible_kind(elig, video_ok, supported)
        for t in targets:
            out.append(
                TargetOut(
                    channel=ch,
                    title=v.title or ch,
                    kind=v.kind,
                    state=state_str,
                    eligible=elig,
                    reason=reason,
                    ineligible_kind=ikind,
                    target_id=str(t.id),
                    target_name=t.name,
                    platform=v.platform,
                    platform_title=v.platform_title,
                    short_label=v.short_label,
                )
            )
    out.sort(key=lambda t: (_PLATFORM_RANK.get(t.platform, len(PLATFORM_ORDER)), t.target_name))
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
    image: str = Form(""),  # URL / Яндекс.Диск / имя файла — альтернатива file upload
    media: list[UploadFile] = File(default=[]),  # noqa: B008
) -> PublishResult:
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    selections = _parse_selections(channels)
    # Отсекаем нерабочие каналы/цели ДО отправки (defense in depth к фронту).
    _vbytes = _upload_video_bytes(media)
    await _reject_ineligible(
        profile_repo,
        profile_id,
        selections,
        content_type,
        session=session,
        has_video=_has_video(media, image),
        video_bytes=_vbytes,
    )

    publication_id = new_publication_id()
    media_paths = await _save_media(media, publication_id)
    if not media_paths and image.strip():
        from crosspost.db.settings_repo import ProfileSettingsRepository

        folder = await ProfileSettingsRepository(session, profile_id=profile_id).get_media_folder()
        try:
            async with _make_http_client() as http:
                fetched = await fetch_media(image.strip(), folder=folder, client=http)
            media_paths = media_store.save_binaries(
                [(fetched.filename, fetched.data)], publication_id
            )
        except MediaSourceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    await _cleanup_if_final(session, profile_id, publication_id, outcomes)
    await _log_outcomes(LogRepository(session), profile_id, outcomes, publication_id)
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
    completed = {Selection(s.channel, s.target) for s in all_now if str(s.status) in _SUCCESS}
    required = set(run.selections)
    if run.article_text:
        required.add(Selection("telegraph"))
    if required <= completed:
        # T-209: media not deleted — retained indefinitely until T-214.
        _RUNS.pop((profile_id, publication_id), None)
        from crosspost.db.sqlite_store import CheckpointStore  # noqa: PLC0415

        await CheckpointStore(session, profile_id=profile_id).delete_checkpoints(publication_id)
    await _log_outcomes(LogRepository(session), profile_id, [outcome], publication_id)
    names = await _target_names(session, profile_id, [outcome.target])
    return _outcome_out(outcome, names)


# ── Scheduled ─────────────────────────────────────────────────────────────────


@router.post("/api/profiles/{profile_id}/scheduled", response_model=ScheduledOut)
async def schedule_post(
    profile_id: int,
    session: SessionDep,
    user: CurrentUser,
    scheduled_at: str = Form(...),
    content_type: str = Form("post"),
    text: str = Form(""),
    title: str | None = Form(None),
    channels: str = Form(...),
    article_text: str = Form(""),  # тело лонгрида → статья Telegraph при исполнении
    link_phrase: str = Form(""),  # фраза ссылки на статью (пусто → «Подробнее»)
    image: str = Form(""),  # URL / Яндекс.Диск / имя файла — альтернатива file upload
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
    _vbytes = _upload_video_bytes(media)
    await _reject_ineligible(
        profile_repo,
        profile_id,
        selections,
        content_type,
        session=session,
        has_video=_has_video(media, image),
        video_bytes=_vbytes,
    )

    key = f"sched-{new_publication_id()}"
    media_paths = await _save_media(media, key)
    if not media_paths and image.strip():
        from crosspost.db.settings_repo import ProfileSettingsRepository

        folder = await ProfileSettingsRepository(session, profile_id=profile_id).get_media_folder()
        try:
            async with _make_http_client() as http:
                fetched = await fetch_media(image.strip(), folder=folder, client=http)
            media_paths = media_store.save_binaries([(fetched.filename, fetched.data)], key)
        except MediaSourceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    content = _build_content(content_type, text, title, media_paths)
    _validate_or_422(content)

    from crosspost.db.models import Role, ScheduledPostStatus
    from crosspost.db.settings_repo import ProfileSettingsRepository

    initial_status = ScheduledPostStatus.SCHEDULED
    if user.role == Role.CLIENT:
        moderation = await ProfileSettingsRepository(
            session, profile_id=profile_id
        ).get_client_moderation()
        if moderation:
            initial_status = ScheduledPostStatus.PENDING_APPROVAL

    post = await pub_repo.create_scheduled(
        content_type=content_type,
        text=text,
        title=title or None,
        media_paths=media_paths,
        channels=channel_list,
        scheduled_at=when,
        article_text=article_text or None,
        link_phrase=link_phrase or None,
        initial_status=initial_status,
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


@router.get("/api/profiles/{profile_id}/scheduled/{scheduled_id}/media/{index}")
async def scheduled_media(profile_id: int, scheduled_id: int, index: int, session: SessionDep):
    """Отдать медиафайл запланированного поста по индексу (для превью в форме).

    Клиент называет только числовой индекс — путь на диске берётся из БД по post_id.
    Конфайнмент: путь должен быть внутри media root; выход за root → 404.
    Изоляция: пост должен принадлежать profile_id; иначе 404.
    """
    import mimetypes

    from starlette.responses import FileResponse

    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    post = await pub_repo.get_scheduled(scheduled_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Пост не найден")

    paths = list(post.media_paths or [])
    if index < 0 or index >= len(paths):
        raise HTTPException(status_code=404, detail="Нет медиафайла с таким индексом")

    candidate = Path(paths[index]).resolve()

    # Конфайнмент: resolved path должен быть внутри media root.
    # Defence in depth даже для путей из БД.
    try:
        candidate.relative_to(media_store._media_root().resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Файл недоступен") from None

    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден на диске")

    mime, _ = mimetypes.guess_type(candidate.name)
    return FileResponse(str(candidate), media_type=mime or "application/octet-stream")


@router.get("/api/profiles/{profile_id}/scheduled/{scheduled_id}", response_model=ScheduledOut)
async def get_scheduled_post(
    profile_id: int, scheduled_id: int, session: SessionDep
) -> ScheduledOut:
    """Получить один запланированный пост (для формы редактирования журнала)."""
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)
    post = await pub_repo.get_scheduled(scheduled_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Запланированный пост не найден")
    return _scheduled_out(post)


class PatchScheduledBody(BaseModel):
    scheduled_at: str | None = None  # ISO-8601
    text: str | None = None
    channels: list | None = None  # list[str] или list[{channel, target_id}]
    image: str | None = None  # http URL / Yandex.Disk link / source-folder filename


# backward-compat alias (старые тесты/клиенты шлют {"scheduled_at": ...})
RescheduleBody = PatchScheduledBody


@router.patch("/api/profiles/{profile_id}/scheduled/{scheduled_id}", response_model=ScheduledOut)
async def reschedule(
    profile_id: int, scheduled_id: int, body: PatchScheduledBody, session: SessionDep
) -> ScheduledOut:
    """Редактировать запланированный пост: текст, цели, время. Изоляция по profile_id."""
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    # 404 если пост не найден или чужой профиль — до CAS, чтобы не палить существование.
    post = await pub_repo.get_scheduled(scheduled_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Запланированный пост не найден")

    # Валидация каналов и capability ДО CAS и ДО скачивания медиа — 422 не меняет данные.
    effective_channels = body.channels if body.channels is not None else list(post.channels or [])
    try:
        selections = selections_from_stored(effective_channels)
    except (TypeError, KeyError) as exc:
        raise HTTPException(status_code=422, detail="channels: некорректный элемент") from exc
    if body.channels is not None or body.image is not None:
        await _reject_ineligible(
            profile_repo, profile_id, selections, post.content_type, session=session
        )

    when: datetime | None = None
    if body.scheduled_at is not None:
        try:
            when = datetime.fromisoformat(body.scheduled_at)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="scheduled_at: ISO-8601 дата/время"
            ) from exc
        # Нормализуем в naive-local (конвенция хранения — как datetime.combine в import_plan):
        # aware → конвертируем в локальное время системы, потом снимаем tzinfo.
        # Голый .replace(tzinfo=None) НЕЛЬЗЯ — он молча сдвигает стену на величину offset.
        when = when.astimezone().replace(tzinfo=None) if when.tzinfo is not None else when
        if when < datetime.now() - timedelta(seconds=60):
            raise HTTPException(status_code=422, detail="scheduled_at: нельзя назначить в прошлое")

    # Разрешаем медиа ДО CAS: если источник невалиден — 422 без записи в БД.
    new_media_paths: list[str] | None = None
    if body.image is not None:
        from crosspost.db.settings_repo import ProfileSettingsRepository

        folder = await ProfileSettingsRepository(session, profile_id=profile_id).get_media_folder()
        try:
            async with _make_http_client() as http:
                fetched = await fetch_media(body.image, folder=folder, client=http)
        except MediaSourceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        key = f"sched-{new_publication_id()}"
        new_media_paths = media_store.save_binaries([(fetched.filename, fetched.data)], key)

    # CAS-апдейт: WHERE status IN ('scheduled','failed') — PUBLISHING/DONE дают rowcount=0.
    updated = await pub_repo.patch_scheduled_cas(
        scheduled_id,
        text=body.text,
        channels=body.channels,
        scheduled_at=when,
        media_paths=new_media_paths,
        rewind_failed=True,
    )
    if not updated:
        # T-209: orphaned new media not deleted — retained indefinitely.
        raise HTTPException(status_code=409, detail="Пост уже исполнен или обрабатывается")

    # T-209: old media not deleted on replace — retained indefinitely until T-214.

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
    await pub_repo.cancel_scheduled(scheduled_id)
    # T-209: media not deleted on cancel — retained indefinitely until T-214.
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
    names = await _target_names(session, profile_id, [s.target for s in statuses])
    results = [
        {
            "channel": s.channel,
            "status": str(s.status),
            "error": s.error or None,
            "target": s.target,
            "target_name": names.get(s.target, ""),
        }
        for s in statuses
    ]
    all_done = all(str(s.status) in _SUCCESS for s in statuses) if statuses else False
    return {"triggered": True, "results": results, "all_done": all_done}


# ── Внутреннее ────────────────────────────────────────────────────────────────


async def _log_outcomes(
    log_repo: LogRepository,
    profile_id: int,
    outcomes,
    publication_id: str,
) -> None:
    """Write a log row for each channel outcome. Failures → ERROR level."""
    for o in outcomes:
        ok = getattr(o, "status", "") in {"done", "submitted"}
        level = "INFO" if ok else "ERROR"
        label = f"{o.channel}·{o.target}" if getattr(o, "target", "") else o.channel
        msg = f"publish: {label} → {o.status}"
        if getattr(o, "external_id", None):
            msg += f" ({o.external_id})"
        if getattr(o, "error", None):
            safe_err = redact_media_tokens(o.error)
            msg += f" — {safe_err}"
        await log_repo.add(
            profile_id,
            channel=o.channel,
            publication_id=publication_id,
            level=level,
            message=msg,
            screenshot_path=getattr(o, "screenshot_path", None),
        )


def _cleanup_paths_safe(paths: list[str]) -> None:
    """Удалить каталоги медиа по списку путей. Идемпотентно, ошибки логируются."""
    keys = {Path(p).parent.name for p in paths if p}
    for key in keys:
        media_store.cleanup_media(key)


async def _reject_ineligible(
    profile_repo: ProfileRepository,
    profile_id: int,
    selections: list[Selection],
    content_type: str,
    *,
    session,
    has_video: bool = False,
    video_bytes: int = 0,
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
        # Видео-правило (тот же хелпер и тексты, что у GET /publish/targets).
        vreason = video_ineligible_reason(
            ch, v.max_video_bytes, has_video=has_video, video_bytes=video_bytes
        )
        if vreason:
            raise HTTPException(status_code=422, detail=vreason)
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
    from crosspost.web.media import MediaUploadError

    try:
        return await media_store.save_uploads(real, key)
    except MediaUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _cleanup_if_final(session, profile_id: int, publication_id: str, outcomes) -> None:
    """Если все каналы завершились — чистим чекпоинты. Медиа не удаляем (T-209)."""
    if all(o.status in _SUCCESS for o in outcomes):
        _RUNS.pop((profile_id, publication_id), None)
        from crosspost.db.sqlite_store import CheckpointStore  # noqa: PLC0415

        await CheckpointStore(session, profile_id=profile_id).delete_checkpoints(publication_id)


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


# ── Модерация клиентских постов (только admin) ────────────────────────────────


class ModerationSetting(BaseModel):
    enabled: bool


@router.get("/api/profiles/{profile_id}/settings/moderation", response_model=ModerationSetting)
async def get_moderation(profile_id: int, session: SessionDep, _: AdminUser) -> ModerationSetting:
    """Настройка режима модерации клиентских постов (только admin)."""
    from crosspost.db.settings_repo import ProfileSettingsRepository

    enabled = await ProfileSettingsRepository(
        session, profile_id=profile_id
    ).get_client_moderation()
    return ModerationSetting(enabled=enabled)


@router.put("/api/profiles/{profile_id}/settings/moderation", response_model=ModerationSetting)
async def set_moderation(
    profile_id: int, body: ModerationSetting, session: SessionDep, _: AdminUser
) -> ModerationSetting:
    """Задать режим модерации: enabled=true → клиентские посты сначала идут на проверку."""
    from crosspost.db.settings_repo import ProfileSettingsRepository

    await ProfileSettingsRepository(session, profile_id=profile_id).set_client_moderation(
        body.enabled
    )
    return body


@router.get("/api/profiles/{profile_id}/scheduled/pending", response_model=list[ScheduledOut])
async def list_pending(profile_id: int, session: SessionDep, _: AdminUser) -> list[ScheduledOut]:
    """Посты, ожидающие одобрения (PENDING_APPROVAL) — только admin."""
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)
    return [_scheduled_out(p) for p in await pub_repo.list_pending_approval()]


@router.post(
    "/api/profiles/{profile_id}/scheduled/{scheduled_id}/approve", response_model=ScheduledOut
)
async def approve_post(
    profile_id: int, scheduled_id: int, session: SessionDep, _: AdminUser
) -> ScheduledOut:
    """Одобрить пост на модерации: PENDING_APPROVAL → SCHEDULED. Только admin."""
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)
    ok = await pub_repo.approve_pending(scheduled_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Пост не найден или уже не на проверке")
    post = await pub_repo.get_scheduled(scheduled_id)
    assert post is not None
    return _scheduled_out(post)


class RejectBody(BaseModel):
    reason: str = ""


@router.post("/api/profiles/{profile_id}/scheduled/{scheduled_id}/reject")
async def reject_post(
    profile_id: int, scheduled_id: int, body: RejectBody, session: SessionDep, _: AdminUser
) -> dict:
    """Отклонить пост на модерации: PENDING_APPROVAL → CANCELLED. Только admin."""
    profile_repo, pub_repo = _repos(session, profile_id)
    await _require_profile(profile_repo, profile_id)

    post = await pub_repo.get_scheduled(scheduled_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Пост не найден")

    ok = await pub_repo.reject_pending(scheduled_id, body.reason)
    if not ok:
        raise HTTPException(status_code=409, detail="Пост не найден или уже не на проверке")

    # T-209: media not deleted on reject — retained indefinitely until T-214.
    return {"rejected": True}


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
        media_paths=list(post.media_paths or []),
        reject_reason=post.reject_reason,
    )
