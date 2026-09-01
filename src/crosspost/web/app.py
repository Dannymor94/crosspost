"""FastAPI application — admin channel-connection panel. Epic 4."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.config import load_config, parse_bool
from crosspost.db.engine import create_engine_and_tables, get_db_url
from crosspost.db.scheduler_repo import SchedulerRepository
from crosspost.scheduler.executor import ScheduledExecutor
from crosspost.web import deps
from crosspost.web.routes import (
    auth,
    channels,
    errors,
    import_plan,
    profiles,
    publish,
    settings,
    tg_login,
    users,
)

logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent / "frontend"


def _ensure_app_logging() -> None:
    """Показывать INFO/WARNING логи crosspost (uvicorn их по умолчанию не выводит).

    Нужно для диагностики (напр. telegram send_code type=…, ошибки исполнителя).
    """
    log = logging.getLogger("crosspost")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    load_config()  # инжектирует runtime/.env в os.environ до любых обращений к секретам
    _ensure_app_logging()
    engine = await create_engine_and_tables(get_db_url())
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(factory)

    # Реконсиляция: посты, застрявшие в PUBLISHING после прошлого падения процесса,
    # вернуть в SCHEDULED (идемпотентность по (publication_id, канал, цель) не даст дублей).
    async with factory() as session:
        await SchedulerRepository(session).reconcile_stuck()

    # Исполнитель расписания: фоновый демон публикует созревшие посты сам.
    # Выключается флагом SCHEDULER_ENABLED=false (напр. в тестах/CLI).
    scheduler_task: asyncio.Task | None = None
    if parse_bool(os.environ.get("SCHEDULER_ENABLED", "true")):
        executor = ScheduledExecutor.from_env(factory)
        app.state.executor = executor
        scheduler_task = asyncio.create_task(executor.run_forever())

    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Crosspost Admin", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(profiles.router)
    app.include_router(channels.router)
    app.include_router(tg_login.router)
    app.include_router(publish.router)
    app.include_router(import_plan.router)
    app.include_router(settings.router)
    app.include_router(errors.router)

    # Serve frontend SPA — must be after API routes
    if _FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def serve_spa() -> FileResponse:
            return FileResponse(_FRONTEND_DIR / "index.html")

    return app


app = create_app()
