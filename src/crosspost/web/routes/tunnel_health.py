"""GET /tunnel-health — per-process nonce for localhost.run tunnel health checks."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from crosspost.tunnel import state

router = APIRouter()


@router.get("/tunnel-health", include_in_schema=False)
async def tunnel_health() -> JSONResponse:
    return JSONResponse({"nonce": state.get_nonce()})
