"""Anonymous, data-minimal endpoints for external availability probes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crosspost.ops.health import HealthConfig, evaluate_readiness

router = APIRouter(tags=["health"])


@router.get("/health/live", include_in_schema=False)
async def live() -> JSONResponse:
    """An HTTP process can respond; does not imply readiness to publish."""
    return JSONResponse({"status": "alive"}, headers={"Cache-Control": "no-store"})


@router.get("/health", include_in_schema=False)
async def ready(request: Request) -> JSONResponse:
    """DB and scheduler-ready, with a separate nonfatal error-spike signal."""
    try:
        config = HealthConfig.from_env()
    except (ValueError, OverflowError):
        return JSONResponse(
            {"status": "unavailable", "error_spike": False},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    app_state = request.app.state
    result = await evaluate_readiness(
        getattr(app_state, "health_factory", None),
        scheduler_enabled=getattr(app_state, "scheduler_enabled", None),
        scheduler_task=getattr(app_state, "scheduler_task", None),
        progress=getattr(app_state, "scheduler_progress", None),
        config=config,
    )
    return JSONResponse(
        {"status": "ok" if result.ready else "unavailable", "error_spike": result.error_spike},
        status_code=200 if result.ready else 503,
        headers={"Cache-Control": "no-store"},
    )
