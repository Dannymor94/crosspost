"""Signed CSRF proof for every unsafe browser request, including anonymous login."""

from __future__ import annotations

import hmac
import os
import re
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from crosspost.auth.sessions import csrf_token
from crosspost.web.auth_guard import production_mode, require_https
from crosspost.web.deps import SESSION_COOKIE

_HEADER = "X-CSRF-Token"
_SAFE = {"GET", "HEAD", "OPTIONS"}
_NONCE = re.compile(r"[A-Za-z0-9_-]{43}")


def enabled() -> bool:
    return production_mode() or os.environ.get("CROSSPOST_CSRF_ENABLED", "") == "true"


def _origin(request: Request) -> str:
    actual = f"{request.url.scheme}://{request.url.netloc}"
    if not production_mode():
        return actual
    expected = os.environ.get("CROSSPOST_AUTH_ORIGIN", "")
    try:
        parts = urlsplit(expected)
    except ValueError:
        raise HTTPException(503, "Авторизация недоступна") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or expected != f"https://{parts.netloc}"
        or actual != expected
    ):
        raise HTTPException(503, "Авторизация недоступна")
    return expected


def _check_origin(request: Request) -> None:
    require_https(request)
    expected = _origin(request)
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
        raise HTTPException(403, "Запрос из другого источника запрещён")
    if origin is not None and origin != expected:
        raise HTTPException(403, "Запрос из другого источника запрещён")
    if referer is not None:
        try:
            parts = urlsplit(referer)
            referer_origin = f"{parts.scheme}://{parts.netloc}"
        except ValueError:
            referer_origin = ""
        if referer_origin != expected:
            raise HTTPException(403, "Запрос из другого источника запрещён")


def _cookie_name(request: Request) -> str:
    return "__Host-crosspost_csrf" if request.url.scheme == "https" else "crosspost_csrf_dev"


def _nonce(request: Request) -> str:
    nonce = request.cookies.get(_cookie_name(request), "")
    return nonce if _NONCE.fullmatch(nonce) else ""


def issue_proof(request: Request, response: Response) -> dict[str, str | bool]:
    response.headers["Cache-Control"] = "no-store"
    if not enabled():
        return {"enabled": False}
    _check_origin(request)
    nonce = _nonce(request)
    if not nonce:
        nonce = secrets.token_urlsafe(32)
        response.set_cookie(
            _cookie_name(request),
            nonce,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
    return {"enabled": True, "token": csrf_token(nonce, request.cookies.get(SESSION_COOKIE, ""))}


def _verify(request: Request) -> None:
    _check_origin(request)
    nonce = _nonce(request)
    proof = request.headers.get(_HEADER, "")
    if not nonce or not re.fullmatch(r"[a-f0-9]{64}", proof):
        raise HTTPException(403, "Обновите страницу и повторите действие")
    expected = csrf_token(nonce, request.cookies.get(SESSION_COOKIE, ""))
    if not hmac.compare_digest(expected, proof):
        raise HTTPException(403, "Обновите страницу и повторите действие")


class CSRFMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in _SAFE:
            request = Request(scope, receive)
            try:
                if enabled():
                    _verify(request)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
