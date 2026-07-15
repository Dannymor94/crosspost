"""Сборка реальных адаптеров под профиль из vault-учёток. Итерация 2а.

telegram   — из JSON-учётки (api_id/api_hash/target_channel/session): свой клиент.
browser    — per-profile storage_state из credentials + идентификаторы из конфига
             (org_id / screen_name / channel_id). Сбор этих идентификаторов в UI —
             следующая итерация; пока берём из runtime/.env (MVP одного владельца).

Нет учётки/подключения → None (сервис пометит канал needs_relogin, не упадёт).
Тяжёлые SDK (Telethon/Playwright) импортируются ЛЕНИВО в своих ветках.
"""

from __future__ import annotations

import logging

from crosspost.adapters.base import ChannelAdapter
from crosspost.channels.telegram_login import parse_credential_blob
from crosspost.channels.validators import VALIDATORS
from crosspost.config import load_config, parse_bool
from crosspost.db.profile_repo import ProfileRepository
from crosspost.orchestrator.task import InMemoryIdempotencyStore

logger = logging.getLogger(__name__)


async def build_profile_adapter(
    repo: ProfileRepository,
    profile_id: int,
    channel: str,
    *,
    store=None,
) -> ChannelAdapter | None:
    """Собрать адаптер канала под профиль. None — нет активной учётки."""
    validator = VALIDATORS.get(channel)
    if validator is None or not validator.enabled:
        return None

    store = store or InMemoryIdempotencyStore()
    session_key = validator.session_channel or channel
    cred = await repo.get_credential(profile_id, session_key, validator.credential_kind)
    if not cred:
        return None  # нет сессии/учётки → сервис отдаст needs_relogin

    if channel == "telegram":
        return await _build_telegram(cred, store)

    return _build_browser(channel, cred, store)


async def _build_telegram(cred: str, store) -> ChannelAdapter | None:
    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    from crosspost.adapters.api.telegram import TelegramAdapter  # noqa: PLC0415

    cfg = parse_credential_blob(cred)
    api_id = int(cfg.get("api_id") or 0)
    api_hash = str(cfg.get("api_hash") or "")
    session = str(cfg.get("session") or "")
    target = str(cfg.get("target_channel") or "")
    if not (api_id and api_hash and session and target):
        logger.warning("telegram: неполная учётка профиля")
        return None

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()  # сессия уже авторизована — без интерактива
    return TelegramAdapter(client, target=target, store=store)


def _build_browser(channel: str, storage_state: str, store) -> ChannelAdapter | None:
    cfg = load_config()
    headless = parse_bool(cfg.get("BROWSER_HEADLESS", "true"))

    if channel == "yandex":
        from crosspost.adapters.browser.yandex import YandexBrowserAdapter  # noqa: PLC0415

        return YandexBrowserAdapter(
            cfg["YANDEX_ORG_ID"], store, headless=headless, storage_state=storage_state
        )
    if channel == "vk_wall":
        from crosspost.adapters.browser.vk_wall import VKWallBrowserAdapter  # noqa: PLC0415

        screen = cfg.get("VK_GROUP_SCREEN_NAME", cfg.get("VK_GROUP_URL", "medithou"))
        return VKWallBrowserAdapter(
            screen, store, headless=headless, storage_state=storage_state
        )
    if channel == "vk_channel":
        from crosspost.adapters.browser.vk_channel import VKChannelBrowserAdapter  # noqa: PLC0415

        return VKChannelBrowserAdapter(
            cfg["VK_CHANNEL_ID"], store, headless=headless, storage_state=storage_state
        )
    return None
