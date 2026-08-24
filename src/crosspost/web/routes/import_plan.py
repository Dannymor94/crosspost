"""Роут массового импорта контент-плана. Трек A, часть 1.

Эндпоинты (под profile_id — изоляция):
  POST .../import/preview — stateless: таблица + имена файлов → отчёт (без БД)
  POST .../import/commit  — таблица + картинки + правило + каналы → ScheduledPost

Ошибки уровня строки НЕ прерывают импорт — возвращаются явно в problems.
Ошибки уровня таблицы (нет колонки, битый формат) → 422.
Правило необязательно, если все строки таблицы имеют явную дату.
"""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from crosspost.channels.validators import VALIDATORS
from crosspost.content.canonical import ContentType
from crosspost.content.capabilities import supports
from crosspost.db.models import ConnectionState
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.publication_repo import PublicationRepository
from crosspost.db.settings_repo import ProfileSettingsRepository
from crosspost.db.target_repo import ChannelTargetRepository
from crosspost.db.vault import get_vault
from crosspost.orchestrator.publish_service import selections_from_stored
from crosspost.orchestrator.task import new_publication_id
from crosspost.scheduler.import_plan import (
    ImportReport,
    PlanImportError,
    RowProblem,
    analyze_headers,
    build_plan,
    parse_rule,
    parse_table,
    peek_headers,
)
from crosspost.scheduler.media_sources import MediaSourceError, fetch_media
from crosspost.scheduler.remote_table import RemoteTableError, fetch_sheet_csv
from crosspost.web import media as media_store
from crosspost.web.deps import SessionDep, require_profile_access

logger = logging.getLogger(__name__)
router = APIRouter(tags=["import"], dependencies=[Depends(require_profile_access)])


# ── Схемы ─────────────────────────────────────────────────────────────────────


class ResolvedOut(BaseModel):
    row_number: int
    file: str
    text: str
    scheduled_at: str  # ISO-8601
    source: str  # "rule" | "explicit"
    has_article: bool = False  # строка несёт article_text → превью покажет бейдж «+ статья»
    media_source: str = ""  # откуда картинка: upload|folder|http|yadisk|""


class ProblemOut(BaseModel):
    row_number: int
    file: str
    reason: str


class PreviewOut(BaseModel):
    resolved: list[ResolvedOut]
    problems: list[ProblemOut]
    warnings: list[ProblemOut]  # файл не найден → пост выйдет без фото
    unmatched_files: list[str]
    resolved_count: int
    problem_count: int
    # Фолбэк распознавания колонок: если обязательная колонка не угадана,
    # превью НЕ падает — просит сопоставить руками (маппинг колонка→поле).
    needs_mapping: bool = False
    headers: list[str] = []  # заголовки таблицы для ручного маппинга
    detected: dict[str, str] = {}  # logical → распознанный заголовок
    missing: list[str] = []  # обязательные логические поля без колонки


class CommitOut(BaseModel):
    created: int
    problems: list[ProblemOut]
    warnings: list[ProblemOut]  # посты без фото (файл не был загружен)
    skipped_count: int  # число строк, не попавших в БД


class SettingsOut(BaseModel):
    media_folder: str = ""  # папка-источник картинок (вариант «имя файла»)
    column_mapping: dict[str, str] = {}  # сохранённый маппинг колонка→поле


class SettingsIn(BaseModel):
    media_folder: str | None = None
    column_mapping: dict[str, str] | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_rule_or_422(
    start_date: str | None,
    times_csv: str | None,
    *,
    interval: str = "daily",
    interval_n: int = 1,
    weekdays_csv: str | None = None,
):
    """None, если оба поля пусты (допустимо при all-explicit таблице)."""
    if not start_date and not times_csv:
        return None
    weekdays: tuple[int, ...] = ()
    if weekdays_csv:
        try:
            weekdays = tuple(int(p) for p in weekdays_csv.split(",") if p.strip())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Дни недели: список чисел 0…6.") from exc
    try:
        return parse_rule(
            start_date or "",
            times_csv or "",
            interval=interval or "daily",
            interval_n=interval_n,
            weekdays=weekdays,
        )
    except PlanImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _parse_table_or_422(data: bytes, filename: str, *, mapping: dict[str, str] | None = None):
    try:
        return parse_table(data, filename, mapping=mapping)
    except PlanImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _load_table_bytes(table: UploadFile | None, sheet_url: str | None) -> tuple[bytes, str]:
    """Источник таблицы: загруженный файл ИЛИ ссылка на Google Таблицу.

    Возвращает (bytes, filename). Ровно один источник должен быть задан.
    Ссылку не логируем (может содержать приватный id таблицы).
    """
    has_file = table is not None and (table.filename or "")
    has_link = bool((sheet_url or "").strip())
    if has_file and has_link:
        raise HTTPException(
            status_code=422, detail="Выберите один источник: файл ИЛИ ссылку на таблицу."
        )
    if has_link:
        try:
            data = await fetch_sheet_csv(sheet_url.strip())  # type: ignore[union-attr]
        except RemoteTableError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return data, "plan.csv"
    if has_file:
        return await table.read(), (table.filename or "plan.csv")  # type: ignore[union-attr]
    raise HTTPException(status_code=422, detail="Приложите таблицу или дайте ссылку на неё.")


def _parse_mapping(column_mapping: str | None) -> dict[str, str]:
    """JSON-поле column_mapping → {logical: заголовок}. Пусто/битое → {}."""
    if not column_mapping:
        return {}
    try:
        parsed = json.loads(column_mapping)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail="column_mapping: ожидается JSON-объект"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="column_mapping: ожидается JSON-объект")
    return {str(k): str(v) for k, v in parsed.items() if v}


def _needs_mapping_preview(headers: list[str], detected_idx: dict[str, int], missing: list[str]):
    """Ответ превью, когда обязательная колонка не распознана — просим маппинг руками."""
    detected_names = {
        logical: headers[idx] for logical, idx in detected_idx.items() if idx < len(headers)
    }
    return PreviewOut(
        resolved=[],
        problems=[],
        warnings=[],
        unmatched_files=[],
        resolved_count=0,
        problem_count=0,
        needs_mapping=True,
        headers=headers,
        detected=detected_names,
        missing=missing,
    )


def _to_preview(report: ImportReport) -> PreviewOut:
    return PreviewOut(
        resolved=[
            ResolvedOut(
                row_number=r.row_number,
                file=r.file,
                text=r.text,
                scheduled_at=r.scheduled_at.isoformat(),
                source=r.source,
                has_article=bool(r.article_text),
                media_source=r.media_source,
            )
            for r in report.resolved
        ],
        problems=[
            ProblemOut(row_number=p.row_number, file=p.file, reason=p.reason)
            for p in report.problems
        ],
        warnings=[
            ProblemOut(row_number=w.row_number, file=w.file, reason=w.reason)
            for w in report.warnings
        ],
        unmatched_files=report.unmatched_files,
        resolved_count=report.matched_count,
        problem_count=len(report.problems),
    )


def _basename_lower(name: str) -> str:
    return re.split(r"[\\/]", (name or "").strip())[-1].lower()


async def _validate_channels(
    profile_repo: ProfileRepository,
    profile_id: int,
    channel_list: list,
    content_type: str,
    *,
    session,
) -> None:
    """Проверка выбора на уровне ЦЕЛИ (мультицели): канал enabled/live/поддерживает,
    а для needs_target — выбранная цель существует (или, для legacy-выбора без
    target_id, заведена хотя бы одна цель; список лениво сидится из старой одиночной).
    """
    try:
        ctype = ContentType(content_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Неизвестный тип: {content_type}") from exc
    try:
        selections = selections_from_stored(channel_list)
    except (TypeError, KeyError) as exc:
        raise HTTPException(status_code=422, detail="channels: некорректный элемент") from exc
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
            raise HTTPException(
                status_code=422,
                detail=f"'{ch}': укажите «{v.target_label.lower()}» в настройках",
            )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/api/profiles/{profile_id}/import/preview", response_model=PreviewOut)
async def import_preview(
    profile_id: int,
    session: SessionDep,
    table: UploadFile | None = File(None),  # noqa: B008
    sheet_url: str | None = Form(None),  # альтернатива файлу: ссылка на Google Таблицу
    filenames: str = Form(...),  # JSON list[str] — имена будущих картинок
    start_date: str | None = Form(None),
    times_csv: str | None = Form(None),
    interval: str = Form("daily"),  # daily | every_n_days | weekly | monthly
    interval_n: int = Form(1),  # шаг для every_n_days
    weekdays_csv: str | None = Form(None),  # для weekly: '0,2,4' (пн,ср,пт)
    column_mapping: str | None = Form(None),  # JSON {logical: заголовок} — ручной маппинг
) -> PreviewOut:
    """Stateless превью: таблица + имена картинок → отчёт связывания без записи в БД.

    Источник таблицы — загруженный файл ИЛИ ссылка на Google Таблицу (sheet_url).
    Если обязательная колонка не распознана — НЕ падаем: возвращаем needs_mapping
    с заголовками для ручного сопоставления. Присланный column_mapping применяется
    и сохраняется для профиля (в следующий раз применится автоматически).
    Возвращает: resolved (что связалось + время), problems, unmatched_files.
    """
    try:
        file_list: list[str] = json.loads(filenames)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail="filenames: ожидается JSON-массив строк"
        ) from exc
    if not isinstance(file_list, list):
        raise HTTPException(status_code=422, detail="filenames: ожидается JSON-массив строк")

    data, filename = await _load_table_bytes(table, sheet_url)

    settings = ProfileSettingsRepository(session, profile_id=profile_id)
    explicit_mapping = _parse_mapping(column_mapping)
    mapping = explicit_mapping or await settings.get_column_mapping()

    # Распознавание колонок с фолбэком — без падения.
    try:
        headers = peek_headers(data, filename)
    except PlanImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    detected_idx, missing = analyze_headers(headers, mapping=mapping)
    if missing:
        return _needs_mapping_preview(headers, detected_idx, missing)

    rows = _parse_table_or_422(data, filename, mapping=mapping)
    rule = _parse_rule_or_422(
        start_date, times_csv,
        interval=interval, interval_n=interval_n, weekdays_csv=weekdays_csv,
    )
    folder = await settings.get_media_folder()
    report = build_plan(rows, file_list, rule, folder_configured=bool(folder))

    # Явно присланный рабочий маппинг запоминаем для профиля.
    if explicit_mapping:
        await settings.set_column_mapping(explicit_mapping)
    return _to_preview(report)


@router.post("/api/profiles/{profile_id}/import/commit", response_model=CommitOut)
async def import_commit(
    profile_id: int,
    session: SessionDep,
    table: UploadFile | None = File(None),  # noqa: B008
    sheet_url: str | None = Form(None),  # альтернатива файлу: ссылка на Google Таблицу
    media: list[UploadFile] = File(default=[]),  # noqa: B008
    channels: str = Form(...),
    content_type: str = Form("post"),
    start_date: str | None = Form(None),
    times_csv: str | None = Form(None),
    interval: str = Form("daily"),  # daily | every_n_days | weekly | monthly
    interval_n: int = Form(1),  # шаг для every_n_days
    weekdays_csv: str | None = Form(None),  # для weekly: '0,2,4' (пн,ср,пт)
    column_mapping: str | None = Form(None),  # JSON {logical: заголовок} — ручной маппинг
) -> CommitOut:
    """Создать ScheduledPost на каждую строку плана (без исполнения).

    Каждая строка получает отдельный медиа-каталог sched-<uuid>.
    Строки с проблемами (файл не загружен, нет даты) пропускаются — остальные создаются.
    Каналы валидируются один раз до начала импорта (live + поддержка content_type).
    """
    try:
        # Вербатим-снимок выбора: старый list[str] ИЛИ новый list[{channel,target_id}].
        channel_list: list = json.loads(channels)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="channels: ожидается JSON-массив") from exc
    if not isinstance(channel_list, list) or not channel_list:
        raise HTTPException(status_code=422, detail="Выберите хотя бы один канал")

    profile_repo = ProfileRepository(session, vault=get_vault())
    pub_repo = PublicationRepository(session, profile_id=profile_id)

    if await profile_repo.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    await _validate_channels(profile_repo, profile_id, channel_list, content_type, session=session)

    data, filename = await _load_table_bytes(table, sheet_url)
    settings = ProfileSettingsRepository(session, profile_id=profile_id)
    explicit_mapping = _parse_mapping(column_mapping)
    mapping = explicit_mapping or await settings.get_column_mapping()
    rows = _parse_table_or_422(data, filename, mapping=mapping)
    if explicit_mapping:
        await settings.set_column_mapping(explicit_mapping)

    # Индекс загруженных файлов: lower(basename) → UploadFile
    real_media = [f for f in (media or []) if getattr(f, "filename", None)]
    available_names = [f.filename for f in real_media if f.filename]  # имена для build_plan
    media_by_basename: dict[str, UploadFile] = {}
    for f in real_media:
        media_by_basename.setdefault(_basename_lower(f.filename or ""), f)

    rule = _parse_rule_or_422(
        start_date, times_csv,
        interval=interval, interval_n=interval_n, weekdays_csv=weekdays_csv,
    )
    folder = await settings.get_media_folder()
    report = build_plan(rows, available_names, rule, folder_configured=bool(folder))

    problems: list[RowProblem] = list(report.problems)
    commit_warnings: list[RowProblem] = list(report.warnings)
    created = 0

    for rrow in report.resolved:
        saved_paths: list[str] = []
        if rrow.file:
            key = f"sched-{new_publication_id()}"
            if rrow.media_source == "upload":
                upload = media_by_basename.get(_basename_lower(rrow.file))
                if upload is None:
                    commit_warnings.append(
                        RowProblem(
                            rrow.row_number,
                            rrow.file,
                            "картинка не загружена — пост выйдет без фото",
                        )
                    )
                else:
                    await upload.seek(0)
                    saved_paths = await media_store.save_uploads([upload], key)
            elif rrow.media_source:
                # Внешний источник (папка/http/Яндекс.Диск) — скачиваем/читаем сейчас.
                try:
                    fetched = await fetch_media(rrow.file, folder=folder)
                    saved_paths = media_store.save_binaries([(fetched.filename, fetched.data)], key)
                except MediaSourceError as exc:
                    commit_warnings.append(
                        RowProblem(rrow.row_number, rrow.file, f"не удалось получить файл: {exc}")
                    )

        await pub_repo.create_scheduled(
            content_type=content_type,
            text=rrow.text,
            title=None,
            media_paths=saved_paths,
            channels=channel_list,
            scheduled_at=rrow.scheduled_at,
            article_text=rrow.article_text or None,
        )
        created += 1

    return CommitOut(
        created=created,
        problems=[
            ProblemOut(row_number=p.row_number, file=p.file, reason=p.reason) for p in problems
        ],
        warnings=[
            ProblemOut(row_number=w.row_number, file=w.file, reason=w.reason)
            for w in commit_warnings
        ],
        skipped_count=len(problems),
    )


@router.get("/api/profiles/{profile_id}/import/settings", response_model=SettingsOut)
async def get_import_settings(profile_id: int, session: SessionDep) -> SettingsOut:
    """Настройки импорта профиля: папка-источник картинок + сохранённый маппинг колонок."""
    settings = ProfileSettingsRepository(session, profile_id=profile_id)
    return SettingsOut(
        media_folder=await settings.get_media_folder(),
        column_mapping=await settings.get_column_mapping(),
    )


@router.put("/api/profiles/{profile_id}/import/settings", response_model=SettingsOut)
async def put_import_settings(
    profile_id: int, session: SessionDep, body: SettingsIn
) -> SettingsOut:
    """Сохранить настройки импорта (папка и/или маппинг). Не заданные поля не трогаем."""
    settings = ProfileSettingsRepository(session, profile_id=profile_id)
    if body.media_folder is not None:
        await settings.set_media_folder(body.media_folder.strip())
    if body.column_mapping is not None:
        await settings.set_column_mapping(body.column_mapping)
    return SettingsOut(
        media_folder=await settings.get_media_folder(),
        column_mapping=await settings.get_column_mapping(),
    )
