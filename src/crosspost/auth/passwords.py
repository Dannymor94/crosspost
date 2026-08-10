"""Хэширование паролей — stdlib scrypt (соль + memory-hard KDF).

Почему scrypt, а не bcrypt/argon2:
  - stdlib (hashlib), ноль сторонних зависимостей — как самописный .env-парсер
    в config.py: держим импорты лёгкими, версии не плодим (CLAUDE.md §минимализм).
  - Свойство безопасности то же, что требовалось: соль внутри, НЕОБРАТИМ,
    сравнение в постоянном времени. При желании перейти на bcrypt/argon2 —
    меняются только эти две функции, формат `scheme$...` уже расширяем.

Формат хранимой строки (всё в одном поле users.password_hash):
    scrypt$<n>$<r>$<p>$<salt_b64>$<dk_b64>
Параметры лежат в самом хэше → верификация не зависит от текущих констант.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

# Параметры KDF. n*r памяти ≈ 128*n*r байт = 16 МиБ при (16384, 8) — под дефолтным
# лимитом OpenSSL (32 МиБ), интерактивный вход ощутимо не тормозит.
_N = 2**14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16
_SCHEME = "scrypt"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode())


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=dklen)


def hash_password(password: str) -> str:
    """Вернуть строку `scrypt$n$r$p$salt$dk` для хранения. Пустой пароль запрещён."""
    if not password:
        raise ValueError("Пароль не может быть пустым")
    salt = os.urandom(_SALT_BYTES)
    dk = _derive(password, salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"{_SCHEME}${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """True, если пароль соответствует хэшу. Любой сбой разбора → False (не бросаем)."""
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != _SCHEME:
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(dk_b64)
        dk = _derive(password, salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False
