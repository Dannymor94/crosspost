"""Минимальная обвязка браузерного тира. Эпик 5.

Предоставляет:
  profile_dir(channel)        — persistent user-data-dir канала (кэш для логина).
  storage_state_path(channel) — файл storageState (куки + localStorage) — ИСТОЧНИК сессии.
  open_page(channel)          — контекст для PUBLISH: свежий контекст со storageState.
  login_context(channel)      — контекст для LOGIN: persistent + экспорт storageState.
  is_logged_in()              — проверка авторизации по URL и/или DOM-маркеру.

Почему storageState, а не persistent-профиль:
  persistent user-data-dir плохо переживает переоткрытие (Яндекс не «прилипает» —
  сессия теряется при следующем запуске). Поэтому источник истины сессии — ФАЙЛ
  storageState (куки + localStorage), который:
    - команда login экспортирует ПОСЛЕ ручного входа (context.storage_state(path=...));
    - адаптер publish загружает в свежий контекст (new_context(storage_state=...)).
  profile_dir остаётся только как кэш user-data-dir на время самого логина.

Все пути берут корень из BROWSER_PROFILES_DIR (один env-var — один источник истины):
    profile_dir("yandex")        → <root>/yandex/
    storage_state_path("yandex") → <root>/yandex_state.json

Паттерн verify_before_retry:
  Адаптер вызывает собственный _find_existing_card(page, text) ПЕРЕД отправкой.
  Если карточка найдена — возвращает DONE/SUBMITTED без дублирования.
  Это закрывает окно «послали → упали до mark_done» (CLAUDE.md инвариант 2).

Лок (user, channel) — asyncio.Lock на имя канала. Мультиюзер — post-MVP.
Playwright импортируется ЛЕНИВО внутри контекст-менеджеров — не тянем в шапку CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

_PROFILES_ROOT_DEFAULT = "runtime/browser_profiles"

# Сентинел «сессию брать из файла» (CLI-режим). UI ВСЕГДА передаёт storage_state
# явно (dict / JSON-строка / None) — из credentials профиля, не из общего файла.
_FROM_FILE: Any = object()


def _profiles_root() -> Path:
    """Корень CLI-браузерных данных. Единственное место чтения BROWSER_PROFILES_DIR."""
    return Path(os.environ.get("BROWSER_PROFILES_DIR", _PROFILES_ROOT_DEFAULT))


def profile_dir(channel: str) -> Path:
    """CLI persistent user-data-dir канала — кэш на время ручного логина.

    ⚠ ОБЩИЙ на все профили — использовать ТОЛЬКО из CLI. UI-путь входа не
    использует persistent-каталог вовсе (browser_login: свежий пустой контекст).
    """
    return _profiles_root() / channel


def storage_state_path(channel: str) -> Path:
    """CLI-файл storageState (куки + localStorage) — источник сессии в CLI-режиме.

    ⚠ ОБЩИЙ на все профили. UI-режим сюда НЕ ходит: его сессия лежит в
    credentials профиля (зашифровано vault'ом) и передаётся в open_page явно.
    """
    return _profiles_root() / f"{channel}_state.json"


def _coerce_storage_state(value: Any) -> Any:
    """Привести storage_state к тому, что понимает Playwright new_context.

    JSON-строка (из credentials) → dict; dict → dict; None → None.
    Путь-строку (CLI) оставляем как есть — Playwright читает файл сам.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith("{"):
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return None
        return value  # трактуем как путь к файлу (CLI-совместимость)
    return None


_channel_locks: dict[str, asyncio.Lock] = {}


def _lock_for(channel: str) -> asyncio.Lock:
    if channel not in _channel_locks:
        _channel_locks[channel] = asyncio.Lock()
    return _channel_locks[channel]


@asynccontextmanager
async def open_page(
    channel: str,
    *,
    headless: bool = False,
    session_channel: str | None = None,
    storage_state: Any = _FROM_FILE,
) -> AsyncGenerator:
    """PUBLISH/VALIDATE: свежий контекст с загруженным storageState.

        # UI: сессия из credentials профиля (расшифрована в памяти)
        async with open_page("vk_wall", headless=True, storage_state=blob) as page: ...
        # CLI: сессия из общего файла (обратная совместимость)
        async with open_page("yandex", headless=True) as page: ...

    Источник сессии:
      - storage_state ПЕРЕДАН явно (UI) → используем его (dict / JSON-строка / None).
        None → пустой контекст (нужен логин). ФАЙЛ НЕ ЧИТАЕМ — изоляция по профилю.
      - storage_state НЕ передан (CLI) → читаем общий storage_state_path(session_key).

    session_channel — из какого канала берётся сессия. Каналы, делящие аккаунт
    (vk_channel через сессию vk_wall), указывают session_channel. Лок — по нему.
    """
    from playwright.async_api import async_playwright  # ленивый импорт

    session_key = session_channel or channel
    if storage_state is _FROM_FILE:
        # CLI-режим: общий файл сессии.
        state_path = storage_state_path(session_key)
        storage_state = str(state_path) if state_path.exists() else None
    else:
        # UI-режим: явная per-profile сессия из credentials.
        storage_state = _coerce_storage_state(storage_state)

    lock = _lock_for(session_key)
    async with lock:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless)
            context = await browser.new_context(
                storage_state=storage_state,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            page = await context.new_page()
            try:
                yield page
            finally:
                await context.close()
                await browser.close()


@asynccontextmanager
async def login_context(
    channel: str,
    *,
    headless: bool = False,
) -> AsyncGenerator:
    """LOGIN (CLI): persistent-контекст для ручного входа + экспорт storageState в файл.

    ⚠ ТОЛЬКО для CLI (`python -m crosspost login`). UI-путь входа — это
    crosspost.channels.browser_login (свежий пустой контекст + сохранение в
    credentials профиля), он сюда НЕ ходит.

    Отдаёт (page, save_state). Вызывающий: открыть логин → дождаться входа руками →
    `await save_state()` (экспорт в storage_state_path) → выйти из блока.

    save_state дёргается ЯВНО (после подтверждения входа), а не в finally,
    чтобы не записать невалидный storageState при обрыве логина.
    """
    from playwright.async_api import async_playwright  # ленивый импорт

    p = profile_dir(channel)
    p.mkdir(parents=True, exist_ok=True)

    lock = _lock_for(channel)
    async with lock:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                str(p),
                headless=headless,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            page = context.pages[0] if context.pages else await context.new_page()

            async def save_state() -> Path:
                state_path = storage_state_path(channel)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(state_path))
                return state_path

            try:
                yield page, save_state
            finally:
                await context.close()


async def is_logged_in(
    page,
    *,
    reject_url_fragments: tuple[str, ...] = (),
    reject_selectors: tuple[str, ...] = (),
    require_selector: str | None = None,
    timeout: int = 5_000,
) -> bool:
    """Проверить авторизацию по URL и DOM-ФАКТАМ (не только по URL).

    Порядок (любой сработавший «не залогинен» → False):
      reject_url_fragments — фрагмент есть в текущем URL (напр. "login", "passport");
      reject_selectors     — на странице ЕСТЬ маркер НЕзалогиненного (форма входа,
                             кнопка «Войти», заголовок «Добро пожаловать»);
      require_selector     — маркер ЗАЛОГИНенного (аватар/топбар) ДОЛЖЕН быть.

    ⚠ Вызывать ТОЛЬКО после реальной загрузки DOM (см. browser_login._settle),
    иначе reject_selectors/require_selector проверяются на пустой странице.
    """
    url: str = page.url
    for fragment in reject_url_fragments:
        if fragment in url:
            return False
    # DOM-факт «НЕ залогинен»: маркер присутствует → точно не вошёл.
    for sel in reject_selectors:
        try:
            found = await page.query_selector(sel)
        except Exception:
            found = None
        if found is not None:
            return False
    if require_selector:
        try:
            await page.wait_for_selector(require_selector, timeout=timeout)
        except Exception:
            return False
    return True
