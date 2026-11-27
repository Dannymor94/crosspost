"""HMAC-подписанные сессии и owner-issued одноразовые recovery-токены.

В куке лежит ТОЛЬКО `user_id:issued_at:signature` — ни пароля, ни секретов.
Подделка user_id ломает подпись (HMAC-SHA256), просроченный токен отвергается.

Секрет — из env SESSION_SECRET, инжектируется снаружи (как VAULT_KEY): при
отсутствии get фейлит явно, без тихого fallback. Смена секрета разлогинивает всех.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import time

_MAX_AGE = 30 * 24 * 3600  # 30 дней
_RECOVERY_MAX_AGE = 15 * 60


def _secret() -> bytes:
    s = os.environ.get("SESSION_SECRET")
    if not s:
        raise RuntimeError(
            "SESSION_SECRET не задан. Задайте env-переменную (случайная длинная строка).\n"
            'Сгенерировать: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    return s.encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def csrf_token(nonce: str, session_cookie: str) -> str:
    """Bind the CSRF proof to the exact browser nonce and session cookie."""
    return _sign(f"csrf-v1:{nonce}:{session_cookie}")


def rate_key(family: str, kind: str, value: str) -> str:
    """Purpose-separated digest; do not persist raw IP, login or capability."""
    return _sign(f"auth-rate-v1:{family}:{kind}:{value}")


def sign_session(user_id: int, *, issued_at: int | None = None, generation: int = 0) -> str:
    """Подписать user_id и версию сессий; старый 3-польный формат = generation 0."""
    ts = int(issued_at if issued_at is not None else time.time())
    if user_id <= 0 or generation < 0:
        raise ValueError("Invalid user/session generation")
    payload = f"{user_id}:{ts}" if generation == 0 else f"{user_id}:{ts}:{generation}"
    raw = f"{payload}:{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_signed(token: str, *, max_parts: int) -> list[str] | None:
    if not token or len(token) > 512:
        return None
    try:
        parts = base64.urlsafe_b64decode(token.encode()).decode().split(":")
        if len(parts) > max_parts:
            return None
        return parts
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        return None


def verify_session_claims(
    token: str, *, max_age: int = _MAX_AGE, now: int | None = None
) -> tuple[int, int] | None:
    """Вернуть (user_id, generation), проверив подпись и строгое окно времени."""
    parts = _decode_signed(token, max_parts=4)
    if parts is None or len(parts) not in (3, 4):
        return None
    try:
        uid, ts = int(parts[0]), int(parts[1])
        generation = int(parts[2]) if len(parts) == 4 else 0
        current = int(now if now is not None else time.time())
        if uid <= 0 or generation < 0 or ts < 0 or not 0 <= current - ts <= max_age:
            return None
        payload = ":".join(parts[:-1])
        if not hmac.compare_digest(_sign(payload), parts[-1]):
            return None
        return uid, generation
    except ValueError:
        return None


def verify_session(token: str, *, max_age: int = _MAX_AGE, now: int | None = None) -> int | None:
    """Совместимый интерфейс проверки подписи; версия проверяется с записью User."""
    claims = verify_session_claims(token, max_age=max_age, now=now)
    return claims[0] if claims else None


def sign_recovery_token(user_id: int, *, generation: int, issued_at: int | None = None) -> str:
    """Админ выдаёт короткоживущий токен; только одна смена версии может победить CAS."""
    if user_id <= 0 or generation < 0:
        raise ValueError("Invalid recovery target")
    ts = int(issued_at if issued_at is not None else time.time())
    payload = f"recover-v1:{user_id}:{generation}:{ts}"
    return base64.urlsafe_b64encode(f"{payload}:{_sign(payload)}".encode()).decode()


def verify_recovery_token(token: str, *, now: int | None = None) -> tuple[int, int] | None:
    """Подлинность, назначение и 15-минутный TTL; одноразовость — CAS в БД."""
    parts = _decode_signed(token, max_parts=5)
    if parts is None or len(parts) != 5 or parts[0] != "recover-v1":
        return None
    try:
        uid, generation, ts = int(parts[1]), int(parts[2]), int(parts[3])
        current = int(now if now is not None else time.time())
        if uid <= 0 or generation < 0 or ts < 0 or not 0 <= current - ts <= _RECOVERY_MAX_AGE:
            return None
        if not hmac.compare_digest(_sign(":".join(parts[:-1])), parts[-1]):
            return None
        return uid, generation
    except ValueError:
        return None
