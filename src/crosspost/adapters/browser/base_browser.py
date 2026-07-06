"""Минимальная обвязка браузерного тира. Эпик 5.

Предоставляет:
  open_page()     — контекст-менеджер: браузер с PERSISTENT профилем, лок, закрытие.
  is_logged_in()  — helper: проверить авторизацию по URL и/или наличию DOM-маркера.

Профиль хранится в BROWSER_PROFILES_DIR/{channel}/ — один профиль на канал,
изолированный, с сохранёнными cookies/sessionStorage. Залогинился один раз —
следующие запуски идут без повторного логина.

Паттерн verify_before_retry:
  Адаптер вызывает собственный _find_existing_card(page, text) ПЕРЕД отправкой.
  Если карточка найдена — возвращает DONE/SUBMITTED без дублирования.
  Это закрывает окно «послали → упали до mark_done» (CLAUDE.md инвариант 2).

Лок (user, channel) — asyncio.Lock на имя канала. Мультиюзер — post-MVP.
Playwright импортируется ЛЕНИВО внутри open_page() — не тянем в шапку CLI.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator


_channel_locks: dict[str, asyncio.Lock] = {}


def _lock_for(channel: str) -> asyncio.Lock:
    if channel not in _channel_locks:
        _channel_locks[channel] = asyncio.Lock()
    return _channel_locks[channel]


@asynccontextmanager
async def open_page(
    channel: str,
    profiles_dir: str | Path,
    *,
    headless: bool = False,
) -> AsyncGenerator:
    """Поднять браузер с персистентным профилем, вернуть страницу.

    Использовать как:
        async with open_page("yandex", cfg["BROWSER_PROFILES_DIR"]) as page:
            await page.goto(...)

    Контекст гасится после выхода из блока. Лок удерживается всё время.
    headless=False по умолчанию — для ручного логина и отладки.
    """
    from playwright.async_api import async_playwright  # ленивый импорт

    profile_path = Path(profiles_dir) / channel
    profile_path.mkdir(parents=True, exist_ok=True)

    lock = _lock_for(channel)
    async with lock:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                str(profile_path),
                headless=headless,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                yield page
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
