"""Загрузка конфигурации из runtime/.env в плоский dict.

Без сторонних зависимостей в шапке: парсер .env на stdlib, чтобы импорт CLI
оставался лёгким и не требовал python-dotenv для тестов. Значения окружения
перекрывают файл (удобно для CI/секрет-сторов).
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_PATH = Path("runtime/.env")

# Ключи API-тира MVP-0 — их разрешено брать прямо из окружения, даже без файла.
_KNOWN_KEYS = [
    "TG_API_ID", "TG_API_HASH", "TG_TARGET_CHANNEL", "TG_SESSION_PATH",
    "VK_ACCESS_TOKEN", "VK_GROUP_ID",
]


def load_config(env_path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Прочитать .env (+ окружение) в dict. Отсутствие файла — не ошибка."""
    cfg: dict[str, str] = {}
    p = Path(env_path)
    if p.exists():
        for raw in p.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.split(" #", 1)[0].strip().strip('"').strip("'")
            cfg[key.strip()] = val
    for key in list(cfg) + _KNOWN_KEYS:
        if key in os.environ:
            cfg[key] = os.environ[key]
    return cfg
