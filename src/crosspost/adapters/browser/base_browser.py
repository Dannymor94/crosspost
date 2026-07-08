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
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

_PROFILES_ROOT_DEFAULT = "runtime/browser_profiles"


def _profiles_root() -> Path:
    """Корень браузерных данных. Единственное место чтения BROWSER_PROFILES_DIR."""
    return Path(os.environ.get("BROWSER_PROFILES_DIR", _PROFILES_ROOT_DEFAULT))


def profile_dir(channel: str) -> Path:
    """Persistent user-data-dir канала — кэш на время ручного логина."""
    return _profiles_root() / channel


def storage_state_path(channel: str) -> Path:
    """Файл storageState (куки + localStorage) — источник истины сессии канала.

    login и publish оба вычисляют путь ЭТОЙ функцией — файл всегда один и тот же.
    """
    return _profiles_root() / f"{channel}_state.json"


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
) -> AsyncGenerator:
    """PUBLISH: свежий контекст с загруженным storageState (если файл есть).

        async with open_page("yandex", headless=True) as page:
            await page.goto(...)

    Сессию НЕ берём из persistent-профиля (он не «прилипает») — берём из
    storage_state_path(channel). Если файла нет — контекст пустой, is_logged_in
    вернёт False → адаптер отдаст NEEDS_RELOGIN.
    """
    from playwright.async_api import async_playwright  # ленивый импорт

    state_path = storage_state_path(channel)
    storage_state = str(state_path) if state_path.exists() else None

    lock = _lock_for(channel)
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
    """LOGIN: persistent-контекст для ручного входа + экспорт storageState.

    Отдаёт кортеж (page, save_state). Вызывающий код:
      1. открывает страницу логина,
      2. ЖДЁТ, пока пользователь войдёт руками и увидит рабочий кабинет,
      3. вызывает `await save_state()` — экспорт куки+localStorage в файл,
      4. выходит из блока — контекст закрывается.

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
    require_selector: str | None = None,
    timeout: int = 5_000,
) -> bool:
    """Проверить авторизацию по URL и/или DOM-маркеру.

    reject_url_fragments — если любой из фрагментов есть в текущем URL,
      считаем пользователя НЕ залогиненным (напр. "passport", "login").
    require_selector — если указан, проверяем его наличие в DOM.
    """
    url: str = page.url
    for fragment in reject_url_fragments:
        if fragment in url:
            return False
    if require_selector:
        try:
            await page.wait_for_selector(require_selector, timeout=timeout)
        except Exception:
            return False
    return True
