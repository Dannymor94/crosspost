"""Админ-консоль ошибок (Трек C): сводка проблем по ВСЕМ клиентам.

Только для admin — оператор видит, что где сломалось, и идёт чинить. Данные берём
из существующей таблицы logs (новых таблиц не заводим). Изоляция — на бэкенде:
весь роутер под require_admin, client получает 403 (не просто скрытие в UI).

Три эндпоинта:
  • GET /api/admin/logs          — лента событий (фильтры + курсорная пагинация);
  • GET /api/admin/logs/summary  — сводка (клиентов с проблемами, топ падающих каналов);
  • GET /api/admin/logs/{id}/screenshot — скриншот сбоя браузерного канала (лениво).
Скриншот НЕ кладём в общий ответ ленты (тяжело) — тянется отдельно по клику.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from crosspost.db.log_repo import LogRepository
from crosspost.web.deps import SessionDep, require_admin

# Весь роутер — только админ. client → 403 на бэкенде, не полагаемся на UI.
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

# «Проблемы» = предупреждения и ошибки. INFO показывается только в режиме «все».
_PROBLEM_LEVELS = {"WARNING", "ERROR"}
_KNOWN_LEVELS = {"INFO", "WARNING", "ERROR"}


class AdminLogOut(BaseModel):
    """Строка ленты админ-консоли. cursor=id (монотонный) для пагинации/инкремента."""

    id: int
    cursor: int
    created_at: str
    profile_id: int
    profile_name: str
    channel: str | None
    level: str
    message: str
    has_screenshot: bool


class ChannelStat(BaseModel):
    channel: str
    count: int


class SummaryOut(BaseModel):
    clients_with_problems: int
    channels: list[ChannelStat]


def _parse_from_time(value: str | None) -> datetime | None:
    """Разобрать границу периода (ISO). Пусто → None (без фильтра), кривой → 422."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="from_time: ожидается ISO-8601") from exc


def _resolve_levels(only_problems: bool, level: str | None) -> set[str] | None:
    """Свести переключатель «только проблемы» и точный фильтр уровня в множество.

    • явный level (INFO/WARNING/ERROR) → ровно он (переопределяет переключатель);
    • only_problems=True без level → {WARNING, ERROR};
    • only_problems=False без level → None (все уровни).
    """
    if level is not None:
        norm = level.upper()
        if norm not in _KNOWN_LEVELS:
            raise HTTPException(status_code=422, detail=f"Неизвестный уровень: {level}")
        return {norm}
    return set(_PROBLEM_LEVELS) if only_problems else None


@router.get("/logs", response_model=list[AdminLogOut])
async def admin_logs(
    session: SessionDep,
    only_problems: bool = True,
    level: str | None = None,
    profile_id: int | None = None,
    channel: str | None = None,
    from_time: str | None = None,
    before: int | None = None,
    since: int | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[AdminLogOut]:
    """Лента событий по всем профилям. По умолчанию — только проблемы (WARNING/ERROR).

    Фильтры: profile_id (клиент), channel, level, from_time (период, created_at >= ).
    Пагинация по id: before — «показать ещё» (старше), since — догрузка новых.
    """
    levels = _resolve_levels(only_problems, level)
    from_dt = _parse_from_time(from_time)
    rows = await LogRepository(session).list_across_profiles(
        levels=levels,
        profile_id=profile_id,
        channel=channel,
        from_time=from_dt,
        before=before,
        since=since,
        limit=limit,
    )
    return [
        AdminLogOut(
            id=lg.id,
            cursor=lg.id,
            created_at=lg.created_at.isoformat(),
            profile_id=lg.profile_id,
            profile_name=name,
            channel=lg.channel,
            level=lg.level,
            message=lg.message,
            has_screenshot=bool(lg.screenshot_path),
        )
        for lg, name in rows
    ]


@router.get("/logs/summary", response_model=SummaryOut)
async def admin_logs_summary(
    session: SessionDep,
    from_time: str | None = None,
) -> SummaryOut:
    """Сводка проблем: сколько клиентов с проблемами и какие каналы падают чаще."""
    from_dt = _parse_from_time(from_time)
    clients, channels = await LogRepository(session).problem_summary(
        levels=set(_PROBLEM_LEVELS), from_time=from_dt
    )
    return SummaryOut(
        clients_with_problems=clients,
        channels=[ChannelStat(channel=ch, count=cnt) for ch, cnt in channels],
    )


@router.get("/logs/{log_id}/screenshot")
async def admin_log_screenshot(log_id: int, session: SessionDep) -> FileResponse:
    """Скриншот сбоя браузерного канала — лениво, по клику (не в общем ответе ленты)."""
    lg = await LogRepository(session).get(log_id)
    if lg is None or not lg.screenshot_path:
        raise HTTPException(status_code=404, detail="Скриншот не найден")
    path = Path(lg.screenshot_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл скриншота недоступен")
    return FileResponse(path, media_type="image/png")
