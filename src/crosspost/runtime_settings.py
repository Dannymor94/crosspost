"""Рантайм-настройки, переключаемые из админки и переживающие рестарт.

Отдельный слой от:
  • config.py — .env/секреты, read-only конфиг развёртывания;
  • vault     — зашифрованные per-profile учётки.
Здесь живут ТОЛЬКО НЕсекретные, процесс-глобальные операционные флаги.

Хранилище — маленький JSON-файл под runtime/ (не коммитится, не секрет). НЕ пишем
в .env: .env — файл секретов, программно его не трогаем. НЕ заводим таблицу в БД:
это операционный тумблер, а не доменные данные (CLAUDE.md: не плодить слои).

Сейчас одна настройка:
  browser_headless — видимость окна браузера при ПУБЛИКАЦИИ. UI-override поверх
  BROWSER_HEADLESS из .env. Логин ВСЕГДА headful независимо от этого (см.
  config.browser_headless) — тумблер на него не влияет.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "runtime/settings.json"
_BROWSER_HEADLESS = "browser_headless"


def _path() -> Path:
    """Путь к файлу настроек. RUNTIME_SETTINGS_PATH переопределяет (тесты/CI)."""
    return Path(os.environ.get("RUNTIME_SETTINGS_PATH", _DEFAULT_PATH))


def _load() -> dict:
    """Прочитать настройки. Отсутствие/битый файл → пустой dict (не роняем старт)."""
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("runtime settings unreadable (%s): %s", p, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def get_browser_headless() -> bool | None:
    """UI-override видимости браузера. None — не задан (тогда решает .env)."""
    val = _load().get(_BROWSER_HEADLESS)
    return val if isinstance(val, bool) else None


def set_browser_headless(value: bool) -> None:
    """Задать UI-override видимости браузера. Переживает рестарт процесса."""
    data = _load()
    data[_BROWSER_HEADLESS] = bool(value)
    _save(data)
