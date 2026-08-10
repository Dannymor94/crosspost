"""Сессия как HMAC-подписанный токен (stateless-кука).

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


def sign_session(user_id: int, *, issued_at: int | None = None) -> str:
    """Подписать user_id → строку-токен для установки в куку."""
    ts = int(issued_at if issued_at is not None else time.time())
    payload = f"{user_id}:{ts}"
    raw = f"{payload}:{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_session(token: str, *, max_age: int = _MAX_AGE, now: int | None = None) -> int | None:
    """Вернуть user_id, если подпись валидна и токен не просрочен; иначе None."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user_id_s, ts_s, sig = raw.split(":")
        payload = f"{user_id_s}:{ts_s}"
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        current = int(now if now is not None else time.time())
        if current - int(ts_s) > max_age:
            return None
        return int(user_id_s)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        return None
