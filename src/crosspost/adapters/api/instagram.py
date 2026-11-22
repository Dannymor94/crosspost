"""Instagram Reels adapter (official Content Publishing API). API-тир.

Публикует ContentType.REEL через video_url (Instagram Login / IGAGW-токены).
Resumable upload (rupload.facebook.com) требует Facebook Login for Business (EAA-токен)
и недоступен для MEDIA_CREATOR / Instagram Login аккаунтов — проверено зондом 2026-10.

Протокол (Meta docs, graph.instagram.com, сверено с реальным API):
  1. POST {graph}/{ig_user_id}/media
        JSON body: media_type=REELS, video_url=<публичный URL>, caption
        Authorization: Bearer <token>
        -> {"id": container_id}
  2. GET {graph}/{container_id}?fields=status_code,status  (опрос до FINISHED)
        status_code ∈ IN_PROGRESS | FINISHED | ERROR | EXPIRED | PUBLISHED
  3. POST {graph}/{ig_user_id}/media_publish
        JSON body: creation_id=<container_id>
        Authorization: Bearer <token>
        -> {"id": media_id}   ← квитанция об успехе (external_id)

video_url — публичный временный подписанный /public-media/{token}, не открытая /media.
INSTAGRAM_PUBLIC_BASE_URL задаёт HTTPS origin (например https://my-host.com).
Без INSTAGRAM_PUBLIC_BASE_URL Instagram-публикации завершатся с явной ошибкой.

Идемпотентность: контейнер создаётся один раз, id сохраняется в store ДО публикации.
PUBLISHED → считаем done. Квитанция (media_id) — НЕ ключ дедупа.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.config import parse_bool
from crosspost.content.canonical import CanonicalContent
from crosspost.orchestrator.task import IdempotencyStore
from crosspost.web.signed_media import redact_media_tokens

logger = logging.getLogger(__name__)

# Версия Graph API — перекрывается env INSTAGRAM_GRAPH_VERSION.
# v26.0 — текущий стабильный (Meta docs, 2026-10). Шейпы эндпоинтов/полей
# сверены с реальным API зондом 2026-10, не угаданы (CLAUDE.md).
DEFAULT_GRAPH_VERSION = "v26.0"

_GRAPH_HOST = "https://graph.instagram.com"

_CONTAINER_STEP = "#container"  # суффикс publication_id для чекпоинта «контейнер создан»

# Reels specs (IG User /media reference). Локально проверяем только то, что дёшево и
# детерминированно без тяжёлых зависимостей: контейнер (расширение) и размер файла.
# Size limit is declared in channels/validators.py VALIDATORS["instagram"].max_video_bytes;
# this fallback is used only if the declaration is missing.
_FALLBACK_MAX_BYTES = 300 * 1024 * 1024  # 300 MB
_ALLOWED_CONTAINERS = frozenset({".mp4", ".mov"})


_ERR_LOCAL_URL = (
    "Instagram не может скачать видео с локального адреса. "
    "Укажите публичный адрес сервера в INSTAGRAM_PUBLIC_BASE_URL (сервер или туннель)."
)

_ERR_TUNNEL_FAILED = (
    "Не удалось поднять туннель для Instagram (localhost.run). "
    "Проверьте интернет/VPN и запустите `make tunnel` вручную."
)

_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost."})


def _is_local_host(host: str) -> bool:
    """True if host is localhost, loopback, or a private/link-local IP."""
    if host.lower() in _LOCAL_HOSTNAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
    except ValueError:
        return False


def _public_base_url() -> str:
    """Validate an external HTTPS origin before publishing a signed media link.

    Reads from tunnel.state (updated at runtime by auto-restart) instead of os.environ
    directly, so a restarted tunnel's new URL takes effect without a server restart.
    """
    from crosspost.tunnel import state as _tstate  # noqa: PLC0415

    val = _tstate.get_public_base_url()
    if not val:
        raise InstagramError(_ERR_LOCAL_URL)
    try:
        parsed = urlsplit(val)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise InstagramError(_ERR_LOCAL_URL) from None
    # A public origin cannot include credentials, media paths, query strings,
    # fragments, malformed ports, ambiguous separators or non-HTTPS schemes.
    if (
        parsed.scheme != "https"
        or not host
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        # urlsplit() returns empty strings for bare delimiters, yet they make
        # the appended media path a query/fragment instead of a URL path.
        or "?" in val
        or "#" in val
        or (port is not None and not 1 <= port <= 65535)
        or any(char in val for char in ("\\", "%", "\r", "\n", "\t", " "))
        or _is_local_host(host)
    ):
        raise InstagramError(_ERR_LOCAL_URL)
    # RFC 1123 hostname labels or globally routable literal IPv4/IPv6 only.
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if not (
            "." in host
            and len(host) <= 253
            and all(
                re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
                for part in host.split(".")
            )
        ):
            raise InstagramError(_ERR_LOCAL_URL) from None
        if host.lower().endswith((".local", ".localhost", ".internal", ".invalid")):
            raise InstagramError(_ERR_LOCAL_URL) from None
    else:
        if not addr.is_global:
            raise InstagramError(_ERR_LOCAL_URL)
    return val.rstrip("/")


# status_code значения опроса контейнера.
_READY = "FINISHED"
_PROCESSING = "IN_PROGRESS"
_PUBLISHED = "PUBLISHED"
_TERMINAL_FAIL = frozenset({"ERROR", "EXPIRED"})


class InstagramError(Exception):
    """Ошибка площадки/валидации — текст пробрасывается в ChannelResult.error дословно."""


class InstagramTokenError(InstagramError):
    """Токен истёк/недействителен (OAuthException / code 190) → needs_relogin."""


def parse_instagram_blob(blob: str) -> tuple[str, str]:
    """Учётка из vault → (access_token, ig_user_id).

    Формат — JSON {"access_token": "...", "ig_user_id": "..."}. Бросает InstagramError
    при неполной/битой учётке (сырые пароли не храним — это долгоживущий токен).
    """
    try:
        data = json.loads(blob)
        token = str(data["access_token"]).strip()
        ig_user_id = str(data["ig_user_id"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InstagramError(f"неполная учётка Instagram: {exc}") from None
    if not token or not ig_user_id:
        raise InstagramError("учётка Instagram: нужны access_token и ig_user_id")
    return token, ig_user_id


def _validate_reel_file(path: Path | str) -> int:
    """Проверить файл против Reels-спеков, определимых локально. Вернуть размер в байтах.

    Бросает InstagramError с явной причиной (fail early, до создания контейнера).
    """
    p = Path(path)
    if not p.exists():
        raise InstagramError(f"файл рилса не найден: {p}")
    if p.suffix.lower() not in _ALLOWED_CONTAINERS:
        raise InstagramError(f"Reels: контейнер должен быть mp4/mov, получен '{p.suffix or '—'}'")
    size = p.stat().st_size
    if size == 0:
        raise InstagramError("Reels: пустой видеофайл")
    from crosspost.channels.validators import VALIDATORS  # noqa: PLC0415

    _v = VALIDATORS.get("instagram")
    max_bytes = (
        _v.max_video_bytes if (_v and _v.max_video_bytes is not None) else _FALLBACK_MAX_BYTES
    )
    if size > max_bytes:
        raise InstagramError(
            f"Reels: файл {size} Б превышает лимит {max_bytes // (1024 * 1024)} МБ"
        )
    return size


# OAuthException codes: 190 = expired/invalid token → needs_relogin.
# Ref: https://developers.facebook.com/docs/graph-api/guides/error-handling
_PERMISSION_NAME = "instagram_business_content_publish"
_PERMISSION_HINT = (
    f"Возможные причины: нет разрешения {_PERMISSION_NAME}, "
    "ограничение приложения Meta или блокировка по региону сети."
)
_REGION_HINT = "Похоже на блокировку по региону: укажите INSTAGRAM_PROXY_URL."


def classify_api_error(err: dict) -> tuple[str, str]:
    """Classify a Graph API error dict → (kind, message).

    kind:
      "token_error"      — code 190: expired/invalid token, re-login needed
      "region_block"     — OAuthException code 200: geo/access block
      "permission_error" — OAuthException other non-190: permission/access issue
      "generic"          — any other error

    message always starts with Meta's verbatim text; OAuthExceptions append _PERMISSION_HINT.
    """
    msg = err.get("message") or str(err)
    code = err.get("code")
    if code == 190:
        return "token_error", msg
    if err.get("type") == "OAuthException":
        full = f"Meta: {msg} (code {code}). {_PERMISSION_HINT}"
        if code == 200:
            return "region_block", full
        return "permission_error", full
    return "generic", msg


def _raise_for_api_error(payload: dict) -> None:
    """Graph error-shape → исключение. OAuthException/190 → токен; иначе — общая ошибка.

    Error-shape: {"error": {"message","type","code", ...}}. Meta's message пробрасываем
    дословно (§errors: без тихой деградации)."""
    err = payload.get("error")
    if not err:
        return
    kind, msg = classify_api_error(err)
    # Meta may echo the video_url (with its signed token) back in an error message;
    # redact before it becomes the exception text that reaches the DB/response.
    msg = redact_media_tokens(msg)
    if kind == "token_error":
        raise InstagramTokenError(msg)
    if kind == "region_block" and not os.environ.get("INSTAGRAM_PROXY_URL"):
        msg = f"{msg} {_REGION_HINT}"
    raise InstagramError(msg)


class InstagramAdapter:
    channel = "instagram"

    def __init__(
        self,
        access_token: str,
        ig_user_id: str,
        store: IdempotencyStore,
        *,
        client=None,
        graph_version: str = DEFAULT_GRAPH_VERSION,
        poll_interval_seconds: float = 30.0,
        poll_timeout_seconds: float = 300.0,
    ) -> None:
        self._token = access_token
        self._ig_user_id = ig_user_id
        self._store = store
        self._client = client  # инъекция httpx.AsyncClient (в тестах — фейк); None → свой
        self._ver = graph_version
        self._poll_interval = poll_interval_seconds
        self._poll_timeout = poll_timeout_seconds
        # ig_user_id — измерение цели в ключах идемпотентности (один аккаунт = одна цель).
        self._target = ig_user_id

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult:
        # §1 идемпотентность: публикация уже прошла → не шлём ничего
        if await self._store.is_done(publication_id, self.channel, target=self._target):
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        # Auto-tunnel: ensure localhost.run is reachable before creating the container.
        if parse_bool(os.environ.get("INSTAGRAM_TUNNEL_AUTO"), default=False):
            try:
                from crosspost.tunnel.manager import ensure_tunnel  # noqa: PLC0415

                await ensure_tunnel()
            except Exception:
                return ChannelResult(self.channel, ResultStatus.FAILED, error=_ERR_TUNNEL_FAILED)

        client = self._client or self._new_client()
        try:
            return await self._run(content, publication_id, client)
        except httpx.ProxyError:
            # Fail closed: proxy set but unreachable → explicit error, no direct fallback.
            return ChannelResult(
                self.channel,
                ResultStatus.FAILED,
                error="Прокси для Instagram недоступен (INSTAGRAM_PROXY_URL). Проверьте, что VPN-клиент запущен.",
            )
        except InstagramTokenError as exc:
            # §errors: отличаем протухший токен → подключение уедет в needs_relogin
            return ChannelResult(self.channel, ResultStatus.NEEDS_RELOGIN, error=str(exc))
        except InstagramError as exc:
            return ChannelResult(self.channel, ResultStatus.FAILED, error=str(exc))
        finally:
            if self._client is None:
                await client.aclose()

    async def _run(self, content, publication_id, client) -> ChannelResult:
        ckpt = publication_id + _CONTAINER_STEP
        container_id = await self._store.get_external_id(ckpt, self.channel, target=self._target)

        if not container_id:
            # Валидируем ДО создания контейнера (fail early, ничего на площадку не ушло).
            path = self._reel_path(content)
            _validate_reel_file(path)
            video_url = self._video_url(path)
            try:
                container_id = await self._create_container(client, content, video_url)
            except httpx.TimeoutException:
                raise InstagramError(
                    "Meta не успела скачать или обработать видео "
                    "(таймаут на шаге создания контейнера)"
                ) from None
            # Чекпоинт ДО публикации: ретрай переиспользует этот контейнер.
            await self._store.mark_done(
                ckpt, self.channel, external_id=container_id, target=self._target
            )
            status = None  # свежий контейнер — опрашиваем с нуля
        else:
            # Ретрай: дожимаем существующий контейнер с его текущего шага.
            try:
                status, detail = await self._status(client, container_id)
            except httpx.TimeoutException:
                raise InstagramError(
                    "Meta не успела скачать или обработать видео "
                    "(таймаут на шаге проверки статуса контейнера)"
                ) from None
            if status == _PUBLISHED:
                await self._store.mark_done(publication_id, self.channel, None, target=self._target)
                return ChannelResult(self.channel, ResultStatus.DONE)
            if status in _TERMINAL_FAIL:
                await self._store.clear_done(ckpt, self.channel, target=self._target)
                return ChannelResult(self.channel, ResultStatus.FAILED, error=detail)

        if status != _READY:
            try:
                status, detail = await self._poll_until_ready(client, container_id)
            except httpx.TimeoutException:
                raise InstagramError(
                    "Meta не успела скачать или обработать видео (таймаут на шаге опроса статуса)"
                ) from None
            if status in _TERMINAL_FAIL:
                await self._store.clear_done(ckpt, self.channel, target=self._target)
                return ChannelResult(self.channel, ResultStatus.FAILED, error=detail)
            if status != _READY:
                # Таймаут обработки видео самой Meta (не httpx-таймаут): контейнер сохранён.
                return ChannelResult(
                    self.channel,
                    ResultStatus.FAILED,
                    error=f"таймаут обработки видео Instagram (>{self._poll_timeout:.0f}s); "
                    "контейнер сохранён, повторите позже",
                )

        try:
            media_id = await self._publish_container(client, container_id)
        except httpx.TimeoutException:
            raise InstagramError(
                "Meta не успела скачать или обработать видео "
                "(таймаут на шаге публикации контейнера)"
            ) from None
        await self._store.mark_done(
            publication_id, self.channel, external_id=media_id, target=self._target
        )
        return ChannelResult(
            self.channel,
            ResultStatus.DONE,
            external_id=media_id,
            url=f"https://www.instagram.com/reel/{media_id}/",
        )

    # ── шаги протокола ──────────────────────────────────────────────────────

    async def _create_container(self, client, content: CanonicalContent, video_url: str) -> str:
        # Confirmed shape via live API probe 2026-10: JSON body + Authorization: Bearer.
        # Resumable upload (upload_type=resumable) is not available for Instagram Login tokens;
        # video_url is used instead (confirmed by live probe).
        safe_url = redact_media_tokens(video_url)
        logger.debug("creating Reels container, video_url: %s", safe_url)
        resp = await client.post(
            f"{_GRAPH_HOST}/{self._ver}/{self._ig_user_id}/media",
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": content.text or "",
            },
        )
        if resp.status_code == 429:
            resp.raise_for_status()
        payload = resp.json()
        _raise_for_api_error(payload)
        container_id = payload.get("id")
        if not container_id:
            raise InstagramError(f"создание контейнера без id: {payload}")
        return str(container_id)

    async def _status(self, client, container_id: str) -> tuple[str, str]:
        """(status_code, status-detail). Опрос контейнера."""
        resp = await client.get(
            f"{_GRAPH_HOST}/{self._ver}/{container_id}",
            params={"fields": "status_code,status"},
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if resp.status_code == 429:
            resp.raise_for_status()
        payload = resp.json()
        _raise_for_api_error(payload)
        code = str(payload.get("status_code") or "")
        raw_status = payload.get("status")  # subcode string from Meta, or null
        if code in _TERMINAL_FAIL:
            if raw_status:
                detail = f"Meta: видео не обработано (status_code={code}, status={raw_status})."
            else:
                detail = (
                    f"Meta: видео не обработано (status_code={code}). "
                    "Проверьте параметры видео или попробуйте другой файл."
                )
        else:
            detail = str(raw_status or code or "неизвестный статус контейнера")
        return code, detail

    async def _poll_until_ready(self, client, container_id: str) -> tuple[str, str]:
        """Опрашивать статус до FINISHED/терминальной ошибки или исчерпания потолка.

        Потолок — по времени (poll_timeout), шаг — poll_interval. Возвращает последний
        (status_code, detail); таймаут вернёт нефинальный статус (вызывающий решит)."""
        attempts = max(1, math.ceil(self._poll_timeout / max(self._poll_interval, 1e-9)))
        status, detail = _PROCESSING, "обработка видео"
        for i in range(attempts):
            status, detail = await self._status(client, container_id)
            if status == _READY or status in _TERMINAL_FAIL:
                return status, detail
            if i < attempts - 1:
                await asyncio.sleep(self._poll_interval)
        return status, detail

    async def _publish_container(self, client, container_id: str) -> str:
        resp = await client.post(
            f"{_GRAPH_HOST}/{self._ver}/{self._ig_user_id}/media_publish",
            headers={"Authorization": f"Bearer {self._token}"},
            json={"creation_id": container_id},
        )
        if resp.status_code == 429:
            resp.raise_for_status()
        payload = resp.json()
        _raise_for_api_error(payload)
        media_id = payload.get("id")
        if not media_id:
            raise InstagramError(f"публикация без media id: {payload}")
        return str(media_id)

    # ── вспомогательное ──────────────────────────────────────────────────────

    def _reel_path(self, content: CanonicalContent) -> Path:
        if not content.media_paths:
            raise InstagramError("Reels: нет видеофайла (media_paths пуст)")
        return Path(content.media_paths[0])

    @staticmethod
    def _video_url(path: Path) -> str:
        """Return a signed public URL Meta can fetch to download the video.

        Requires INSTAGRAM_PUBLIC_BASE_URL (public server) and MEDIA_URL_SECRET
        (signing key). Raises InstagramError when INSTAGRAM_PUBLIC_BASE_URL is
        missing or points to a local address.
        """
        from crosspost.web.signed_media import sign_media_path  # noqa: PLC0415

        base = _public_base_url()  # validates not-empty and not-local
        media_root = Path(os.environ.get("MEDIA_DIR", "runtime/media"))
        try:
            rel = path.relative_to(media_root.resolve())
        except ValueError:
            rel = Path(path.name)
        token = sign_media_path(rel.as_posix())
        return f"{base}/public-media/{token}"

    @staticmethod
    def _new_client():
        import httpx

        # Загрузка видео может идти долго → щедрый таймаут на запись/чтение.
        kwargs: dict = {"timeout": httpx.Timeout(300.0, connect=30.0)}
        proxy_url = os.environ.get("INSTAGRAM_PROXY_URL", "")
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = InstagramAdapter  # noqa: E305
