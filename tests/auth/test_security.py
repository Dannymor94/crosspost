"""Тесты примитивов авторизации: хэш паролей (scrypt) и подпись сессии (HMAC).

Ключевое требование безопасности: хэш пароля НЕОБРАТИМ и солёный;
подделка сессионного токена не проходит проверку подписи.
"""

from __future__ import annotations

import pytest

from crosspost.auth.passwords import hash_password, verify_password
from crosspost.auth.sessions import sign_session, verify_session

# ── Пароли ────────────────────────────────────────────────────────────────────


def test_hash_roundtrip():
    h = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", h) is True
    assert verify_password("wrong", h) is False


def test_hash_is_irreversible_and_salted():
    """Хэш не содержит пароль и различается при каждом вызове (соль)."""
    pwd = "correct horse battery staple"
    h1 = hash_password(pwd)
    h2 = hash_password(pwd)
    assert pwd not in h1  # пароль не восстановить из строки хэша
    assert h1 != h2  # разная соль → разные хэши одного пароля
    assert h1.startswith("scrypt$")
    # но оба валидируют исходный пароль
    assert verify_password(pwd, h1) and verify_password(pwd, h2)


def test_empty_password_rejected():
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_garbage_hash_is_false():
    assert verify_password("x", "not-a-valid-hash") is False
    assert verify_password("x", "") is False


# ── Сессии ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _session_secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-please-change")


def test_session_sign_verify_roundtrip():
    token = sign_session(42)
    assert verify_session(token) == 42


def test_session_tamper_breaks_signature():
    """Подмена user_id в токене ломает HMAC-подпись → None."""
    token = sign_session(1)
    import base64

    raw = base64.urlsafe_b64decode(token.encode()).decode()
    uid, ts, sig = raw.split(":")
    forged_raw = f"999:{ts}:{sig}"  # подменили user_id, подпись прежняя
    forged = base64.urlsafe_b64encode(forged_raw.encode()).decode()
    assert verify_session(forged) is None


def test_session_expired():
    token = sign_session(7, issued_at=1_000)
    # now намного позже issued_at + max_age → просрочен
    assert verify_session(token, max_age=100, now=10_000) is None
    # в пределах окна — валиден
    assert verify_session(token, max_age=100, now=1_050) == 7


def test_session_wrong_secret(monkeypatch):
    token = sign_session(5)
    monkeypatch.setenv("SESSION_SECRET", "a-different-secret")
    assert verify_session(token) is None


def test_session_empty_or_garbage():
    assert verify_session("") is None
    assert verify_session("!!!not-base64!!!") is None
