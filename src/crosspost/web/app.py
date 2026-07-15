"""FastAPI application — admin channel-connection panel. Epic 4."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crosspost.db.engine import create_engine_and_tables, get_db_url
from crosspost.web import deps
from crosspost.web.routes import channels, profiles, publish, tg_login

_FRONTEND_DIR = Path(__file__).parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    engine = await create_engine_and_tables(get_db_url())
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    deps.set_session_factory(factory)
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Crosspost Admin", lifespan=lifespan)
    app.include_router(profiles.router)
    app.include_router(channels.router)
    app.include_router(tg_login.router)
    app.include_router(publish.router)

    # Serve frontend SPA — must be after API routes
    if _FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def serve_spa() -> FileResponse:
            return FileResponse(_FRONTEND_DIR / "index.html")

    return app


app = create_app()
