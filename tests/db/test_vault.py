"""Тесты vault-слоя: Fernet-шифрование credentials.blob.

Покрываем:
  - round-trip: encrypt → decrypt возвращает исходную строку
  - в БД лежит шифртекст (не равен plaintext, не читается без ключа)
  - отсутствие VAULT_KEY → явная ошибка при вызове get_vault()
  - неверный ключ → ошибка расшифровки
  - разные шифртексты для одного plaintext (Fernet nonce-based)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import Credential, CredentialKind, Profile

# ── фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture
def valid_key(monkeypatch) -> str:
    """Генерируем валидный Fernet-ключ и прописываем в env."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VAULT_KEY", key)
    return key


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("VAULT_KEY", raising=False)


@pytest_asyncio.fixture
async def session():
    engine = await create_engine_and_tables("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


# ── тесты vault-функций ──────────────────────────────────────────────────────


def test_round_trip(valid_key):
    """encrypt → decrypt возвращает исходную строку."""
    from crosspost.db.vault import decrypt_blob, encrypt_blob, get_vault

    vault = get_vault()
    plaintext = '{"cookies": [], "origins": []}'
    ciphertext = encrypt_blob(vault, plaintext)
    assert decrypt_blob(vault, ciphertext) == plaintext


def test_ciphertext_is_not_plaintext(valid_key):
    """В поле blob лежит не открытый текст."""
    from crosspost.db.vault import encrypt_blob, get_vault

    vault = get_vault()
    plaintext = "super-secret-token"
    ciphertext = encrypt_blob(vault, plaintext)
    assert plaintext not in ciphertext
    assert plaintext.encode() not in ciphertext.encode("latin-1", errors="replace")


def test_each_encryption_is_unique(valid_key):
    """Fernet использует nonce — один plaintext даёт разные шифртексты."""
    from crosspost.db.vault import encrypt_blob, get_vault

    vault = get_vault()
    a = encrypt_blob(vault, "same")
    b = encrypt_blob(vault, "same")
    assert a != b


def test_missing_vault_key_raises(no_key):
    """Если VAULT_KEY не задан — явная ошибка, не молчаливый fallback."""
    import importlib

    import crosspost.db.vault as vault_mod

    importlib.reload(vault_mod)
    with pytest.raises(RuntimeError, match="VAULT_KEY"):
        vault_mod.get_vault()


def test_wrong_key_raises_on_decrypt(valid_key):
    """Расшифровка чужим ключом бросает исключение."""
    from cryptography.fernet import Fernet, InvalidToken

    from crosspost.db.vault import decrypt_blob, encrypt_blob, get_vault

    vault = get_vault()
    ciphertext = encrypt_blob(vault, "secret")

    other_key = Fernet.generate_key()
    other_vault = Fernet(other_key)
    with pytest.raises(InvalidToken):
        decrypt_blob(other_vault, ciphertext)


# ── тест интеграции с БД ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blob_in_db_is_ciphertext(session, valid_key):
    """В колонке blob хранится шифртекст, а не open-text."""
    from sqlalchemy import text

    from crosspost.db.vault import encrypt_blob, get_vault

    # создаём профиль
    p = Profile(name="test")
    session.add(p)
    await session.commit()
    await session.refresh(p)

    plaintext = "tg-session-string-value"
    vault = get_vault()
    ciphertext = encrypt_blob(vault, plaintext)

    cred = Credential(
        profile_id=p.id,
        channel="telegram",
        kind=CredentialKind.API_TOKEN,
        blob=ciphertext,
    )
    session.add(cred)
    await session.commit()

    # читаем raw blob напрямую из SQLite — plaintext там быть не должно
    row = (await session.execute(text("SELECT blob FROM credentials LIMIT 1"))).first()
    assert row is not None
    raw_blob = row[0]
    assert plaintext not in raw_blob
    assert raw_blob != plaintext

    # но через decrypt — получаем обратно
    from crosspost.db.vault import decrypt_blob

    assert decrypt_blob(vault, raw_blob) == plaintext
