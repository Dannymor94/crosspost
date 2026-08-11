"""Глобальные операционные настройки (только админ).

Сейчас — видимость окна браузера при публикации (headless on/off). Значение
переживает рестарт (runtime_settings) и действует со следующей публикации без
перезапуска. Логин на это НЕ влияет — он всегда headful (config.browser_headless).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from crosspost import runtime_settings
from crosspost.config import browser_headless
from crosspost.web.deps import AdminUser

router = APIRouter(prefix="/api/settings", tags=["settings"])


class BrowserVisibility(BaseModel):
    headless: bool  # true → окно скрыто; false → окно видно


@router.get("/browser", response_model=BrowserVisibility)
async def get_browser_visibility(_: AdminUser) -> BrowserVisibility:
    """Текущий ЭФФЕКТИВНЫЙ режим (override поверх .env)."""
    return BrowserVisibility(headless=browser_headless())


@router.put("/browser", response_model=BrowserVisibility)
async def set_browser_visibility(body: BrowserVisibility, _: AdminUser) -> BrowserVisibility:
    """Задать override видимости браузера. Возвращает новый эффективный режим."""
    runtime_settings.set_browser_headless(body.headless)
    return BrowserVisibility(headless=browser_headless())
