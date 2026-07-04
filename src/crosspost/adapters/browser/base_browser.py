"""Минимальная обвязка браузерного тира. Эпик 5.

Одна функция: запустить Playwright с персистентным профилем и отдать страницу.
Профиль хранится в BROWSER_PROFILES_DIR/{channel}/ — один профиль на канал,
изолированный, с сохранёнными cookies/sessionStorage. Залогинился один раз —
следующие запуски идут без повторного логина.

Лок (user, channel) — asyncio.Lock на экземпляр адаптера. Один адаптер =
одна задача в момент времени (CLAUDE.md инвариант 5). Мультиюзер и персистентный
хранилище локов — post-MVP.

Playwright импортируется ЛЕНИВО внутри open_page() — не тянем пакет на уровне
CLI-импорта (браузерный тир не входит в install-mvp).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncGenerator

from contextlib import asynccontextmanager


# Лок на уровне модуля — один слот на (channel-имя). При мультиюзере заменить
# на dict[(user_id, channel)] -> Lock, но это post-MVP.
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
        async with open_page("vk", cfg["BROWSER_PROFILES_DIR"]) as page:
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
                # базовые настройки против детекции бота
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                yield page
            finally:
                await context.close()
