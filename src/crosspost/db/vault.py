"""Vault-слой: шифрование credentials.blob. Слой 0.2.

Использует Fernet (AES-128-CBC + HMAC-SHA256) из пакета cryptography.
НЕ изобретает крипту — только тонкая обёртка.

Граница доступа: vault импортируется ТОЛЬКО из воркеров/ядра (adapters, orchestrator).
UI-слой (web/) к vault не обращается напрямую — данные проходят через ядро.

Ключ:
  - Берётся из env VAULT_KEY (base64url-encoded 32 байта, формат Fernet).
  - Генерация: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  - НЕ хранится в runtime/ рядом с БД. Инжектируется снаружи (env/keyring/KMS).
  - Если VAULT_KEY отсутствует — get_vault() бросает RuntimeError. Нет молчаливого fallback.

Расшифрованные данные:
  - Живут в памяти коротко (возвращаются из decrypt_blob, не кешируются).
  - Не логируются, не пишутся на диск.
  - Не передаются в UI-слой.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet


def get_vault() -> Fernet:
    """Вернуть Fernet-объект с ключом из VAULT_KEY.

    Бросает RuntimeError, если VAULT_KEY не задан — явная ошибка, не тихий fallback.
    Вызывать при старте воркера/CLI, не при каждом запросе (объект можно держать в памяти).
    """
    key = os.environ.get("VAULT_KEY")
    if not key:
        raise RuntimeError(
            "VAULT_KEY не задан. Задайте env-переменную с Fernet-ключом.\n"
            'Сгенерировать: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode())


def encrypt_blob(vault: Fernet, plaintext: str) -> str:
    """Зашифровать строку, вернуть base64url-строку (safe to store in TEXT column)."""
    return vault.encrypt(plaintext.encode()).decode()


def decrypt_blob(vault: Fernet, ciphertext: str) -> str:
    """Расшифровать строку. Бросает cryptography.fernet.InvalidToken при неверном ключе."""
    return vault.decrypt(ciphertext.encode()).decode()
