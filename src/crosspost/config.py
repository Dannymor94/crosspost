"""Загрузка конфигурации из runtime/.env в плоский dict.

Без сторонних зависимостей в шапке: парсер .env на stdlib, чтобы импорт CLI
оставался лёгким и не требовал python-dotenv для тестов. Значения окружения
перекрывают файл (удобно для CI/секрет-сторов).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)

DEFAULT_ENV_PATH = Path("runtime/.env")

_FALSY = {"false", "0", "no", "off", ""}


def parse_bool(value: str | None, default: bool = True) -> bool:
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


def load_config(env_path: str | Path | None = None) -> dict[str, str]:
    """Прочитать .env (+ окружение) в dict и вставить в os.environ.

    os.environ перекрывает файл (удобно для CI/секрет-сторов).
    Уже установленные переменные окружения НЕ перезаписываем (setdefault).
    env_path=None резолвится в DEFAULT_ENV_PATH в момент вызова, а не при импорте —
    это позволяет тестам подменять DEFAULT_ENV_PATH через monkeypatch.setattr.
    """
    cfg: dict[str, str] = {}
    p = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    if p.exists():
        for raw in p.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.split(" #", 1)[0].strip().strip('"').strip("'")
            cfg[key.strip()] = val
    # os.environ wins over .env for every key; keys only in env are also included.
    cfg.update(os.environ)
    # Deprecated Telegram var aliases: TG_API_ID → TELEGRAM_API_ID (T-106).
    # Empty canonical is treated as "not set"; alias is applied to both cfg and os.environ
    # (setdefault below won't overwrite an empty string already in os.environ).
    for canonical, legacy in (
        ("TELEGRAM_API_ID", "TG_API_ID"),
        ("TELEGRAM_API_HASH", "TG_API_HASH"),
    ):
        if legacy in cfg and not cfg.get(canonical):
            _log.warning("%s is deprecated, use %s", legacy, canonical)
            cfg[canonical] = cfg[legacy]
            os.environ[canonical] = cfg[legacy]  # force-write: empty string must be replaced
    # Inject .env-only keys into os.environ (setdefault — don't overwrite external env).
    for key, val in cfg.items():
        os.environ.setdefault(key, val)
    return cfg
