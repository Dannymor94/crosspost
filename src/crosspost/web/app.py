"""FastAPI application — admin channel-connection panel. Epic 4."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.config import load_config, parse_bool
from crosspost.db.engine import create_engine_and_tables, get_db_url
from crosspost.db.scheduler_repo import SchedulerRepository
from crosspost.ops.health import ErrorSpikeCache, SchedulerProgress
from crosspost.scheduler.executor import ScheduledExecutor
from crosspost.web import deps
from crosspost.web.csrf import CSRFMiddleware
from crosspost.web.routes import (
    auth,
    calendar_actions,
    channels,
    client_connection,
    drafts,
    errors,
    health,
    history,
    import_plan,
    invitations,
    profiles,
    public_media,
    publish,
    settings,
    support,
    telegram_discovery,
    tg_login,
    tunnel_health,
    users,
)
from crosspost.web.signed_media import redact_media_tokens

logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent / "frontend"


class _RedactPublicMediaFilter(logging.Filter):
    """Truncate signed tokens in uvicorn access logs: /public-media/<8chars>…"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                redact_media_tokens(a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        record.msg = redact_media_tokens(str(record.msg))
        return True


def _ensure_app_logging() -> None:
    """Показывать INFO/WARNING логи crosspost (uvicorn их по умолчанию не выводит).

    Нужно для диагностики (напр. telegram send_code type=…, ошибки исполнителя).
    Также редактирует uvicorn.access: подписанные токены в /public-media/ усекаются.
    """
    log = logging.getLogger("crosspost")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False

    # Redact signed tokens from uvicorn access log (full token must not appear in logs).
    access_log = logging.getLogger("uvicorn.access")
    for f in access_log.filters:
        if isinstance(f, _RedactPublicMediaFilter):
            break
    else:
        access_log.addFilter(_RedactPublicMediaFilter())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    import secrets as _secrets

    load_config()  # инжектирует runtime/.env в os.environ до любых обращений к секретам
    _ensure_app_logging()
    # MEDIA_URL_SECRET НЕ проверяем на старте: деплой без Instagram должен подниматься.
    # Отсутствие секрета всплывёт явной ошибкой при первом подписании/проверке медиа-URL
    # (signed_media._secret() → RuntimeError) — только у тех, кто реально постит в Instagram.

    # Tunnel nonce: random per-process value, returned by /tunnel-health.
    from crosspost.tunnel import state as _tunnel_state  # noqa: PLC0415

    _tunnel_state.set_nonce(_secrets.token_hex(16))
    _tunnel_state.set_public_base_url(os.environ.get("INSTAGRAM_PUBLIC_BASE_URL", ""))
    engine = await create_engine_and_tables(get_db_url())
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(factory)
    app.state.health_factory = factory
    app.state.health_error_cache = ErrorSpikeCache()

    # Реконсиляция: посты, застрявшие в PUBLISHING после прошлого падения процесса,
    # вернуть в SCHEDULED (идемпотентность по (publication_id, канал, цель) не даст дублей).
    async with factory() as session:
        await SchedulerRepository(session).reconcile_stuck()

    # Исполнитель расписания: фоновый демон публикует созревшие посты сам.
    # Выключается флагом SCHEDULER_ENABLED=false (напр. в тестах/CLI).
    scheduler_task: asyncio.Task | None = None
    scheduler_enabled = parse_bool(os.environ.get("SCHEDULER_ENABLED", "true"))
    app.state.scheduler_enabled = scheduler_enabled
    app.state.scheduler_task = None
    app.state.scheduler_progress = None
    if scheduler_enabled:
        progress = SchedulerProgress()
        executor = ScheduledExecutor.from_env(factory, progress=progress)
        app.state.executor = executor
        app.state.scheduler_progress = progress
        scheduler_task = asyncio.create_task(executor.run_forever())
        app.state.scheduler_task = scheduler_task

    # Фоновый health-check сессий: проверяет LIVE-подключения, переводит протухшие
    # в needs_relogin до публикации. Выключается флагом HEALTH_CHECK_ENABLED=false.
    health_task: asyncio.Task | None = None
    if parse_bool(os.environ.get("HEALTH_CHECK_ENABLED", "true")):
        from crosspost.sessions.health import HealthChecker  # noqa: PLC0415

        health_checker = HealthChecker.from_env(factory)
        health_task = asyncio.create_task(health_checker.run_forever())

    try:
        yield
    finally:
        if health_task is not None:
            health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await health_task
        if scheduler_task is not None:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
        app.state.scheduler_task = None
        app.state.scheduler_enabled = None
        app.state.health_factory = None
        app.state.health_error_cache = None
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Crosspost Admin", lifespan=lifespan)
    app.add_middleware(CSRFMiddleware)
    app.include_router(auth.router)
    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(invitations.router)
    app.include_router(support.router)
    app.include_router(profiles.router)
    app.include_router(channels.router)
    app.include_router(client_connection.router)
    app.include_router(telegram_discovery.router)
    app.include_router(tg_login.router)
    app.include_router(publish.router)
    app.include_router(drafts.router)
    app.include_router(history.router)
    app.include_router(calendar_actions.router)
    app.include_router(import_plan.router)
    app.include_router(settings.router)
    app.include_router(errors.router)
    app.include_router(public_media.router)
    app.include_router(tunnel_health.router)

    # Serve frontend SPA — must be after API routes
    if _FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")

        @app.get("/pwa/config", include_in_schema=False)
        async def pwa_config() -> JSONResponse:
            return JSONResponse(
                {"enabled": os.environ.get("CROSSPOST_PWA_ENABLED", "") == "true"},
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/pwa/manifest.webmanifest", include_in_schema=False)
        async def pwa_manifest() -> FileResponse:
            if os.environ.get("CROSSPOST_PWA_ENABLED", "") != "true":
                raise HTTPException(404)
            return FileResponse(
                _FRONTEND_DIR / "pwa/manifest.webmanifest",
                media_type="application/manifest+json",
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/pwa/sw.js", include_in_schema=False)
        async def pwa_worker() -> FileResponse:
            if os.environ.get("CROSSPOST_PWA_ENABLED", "") != "true":
                raise HTTPException(404)
            return FileResponse(
                _FRONTEND_DIR / "pwa/sw.js",
                media_type="application/javascript",
                headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-store"},
            )

        @app.get("/", include_in_schema=False)
        async def serve_spa() -> FileResponse:
            return FileResponse(_FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-store"})

        @app.get("/account", include_in_schema=False)
        async def serve_account() -> FileResponse:
            return FileResponse(
                _FRONTEND_DIR / "account.html",
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": (
                        "default-src 'none'; script-src 'self'; style-src 'self'; "
                        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                        "base-uri 'none'"
                    ),
                },
            )

        client_html = _FRONTEND_DIR / "client.html"
        if client_html.exists():

            @app.get("/client", include_in_schema=False)
            async def serve_client_spa() -> FileResponse:
                return FileResponse(client_html, headers={"Cache-Control": "no-store"})

    return app


app = create_app()
