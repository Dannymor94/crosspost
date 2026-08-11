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
    "TG_API_ID",
    "TG_API_HASH",
    "TG_TARGET_CHANNEL",
    "TG_SESSION_PATH",
    "VK_ACCESS_TOKEN",
    "VK_GROUP_ID",
    "VK_PHOTO_UPLOAD_ENABLED",
]


_FALSY = {"false", "0", "no", "off", ""}


def parse_bool(value: str, default: bool = True) -> bool:
    """Разобрать строковое значение флага из .env в bool.

    "false"/"0"/"no"/"off"/"" → False.  Всё остальное → True.
    Регистронезависимо. default используется только если value is None.
    """
    if value is None:
        return default
    return value.strip().lower() not in _FALSY


def browser_headless() -> bool:
    """Режим браузера для ПУБЛИКАЦИИ (open_page). Единственный источник истины.

    Флаг BROWSER_HEADLESS, по умолчанию False — локальная отладка: окно видно,
    удобно ловить смену DOM/капчу глазами. На сервере ставится BROWSER_HEADLESS=true.

    ⚠ Применяется ТОЛЬКО к публикации. ЛОГИН (login_context / browser_login)
    ВСЕГДА headful независимо от флага: пользователь обязан видеть окно, чтобы
    войти руками (QR/код) — авто-логин паролем запрещён (CLAUDE.md).

    Xvfb-задел (сервер, пока НЕ реализуем): если площадка палит headless-Chromium
    антиботом, на headless-сервере гоняем headful-Chromium под виртуальным
    дисплеем — `xvfb-run -a uvicorn ...` при BROWSER_HEADLESS=false. Тогда окно
    «есть» для Chromium, но невидимо на безголовой машине. Логину это тоже даёт
    среду для headful-входа по VNC/удалёнке.

    Приоритет: UI-override (runtime_settings, тумблер в админке) → .env → дефолт.
    Читается ПРИ КАЖДОЙ сборке адаптера, поэтому тумблер действует со следующей
    публикации без рестарта процесса.
    """
    from crosspost import runtime_settings  # ленивый импорт: config — низкоуровневый

    override = runtime_settings.get_browser_headless()
    if override is not None:
        return override
    return parse_bool(load_config().get("BROWSER_HEADLESS", "false"), default=False)


def load_config(env_path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Прочитать .env (+ окружение) в dict и вставить в os.environ.

    os.environ перекрывает файл (удобно для CI/секрет-сторов).
    Уже установленные переменные окружения НЕ перезаписываем (setdefault).
    """
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
    # os.environ побеждает файл.
    for key in list(cfg) + _KNOWN_KEYS:
        if key in os.environ:
            cfg[key] = os.environ[key]
    # Инжектируем в os.environ (setdefault — не затираем то, что задано снаружи).
    for key, val in cfg.items():
        os.environ.setdefault(key, val)
    return cfg
