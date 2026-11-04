"""Сборка реальных адаптеров под профиль из vault-учёток. Итерация 2а.

telegram   — из JSON-учётки (api_id/api_hash/target_channel/session): свой клиент.
browser    — per-profile storage_state (сессия) + per-profile ЦЕЛЬ постинга
             (CredentialKind.TARGET: screen_name / org_id / channel_id). Даже при
             ОБЩЕМ аккаунте у каждого профиля СВОЯ группа — изоляция клиентов.
             Цель НЕ берётся из env: без per-profile цели постили бы в чужую группу.

Нет сессии ИЛИ нет цели → None (сервис пометит канал needs_relogin, не упадёт).
Тяжёлые SDK (Telethon/Playwright) импортируются ЛЕНИВО в своих ветках.
"""

from __future__ import annotations

import logging
import os

from crosspost.adapters.base import ChannelAdapter
from crosspost.channels.telegram_login import parse_credential_blob
from crosspost.channels.validators import VALIDATORS, normalize_target
from crosspost.config import browser_headless
from crosspost.db.models import CredentialKind
from crosspost.db.profile_repo import ProfileRepository
from crosspost.db.sqlite_store import CheckpointStore
from crosspost.db.target_repo import ChannelTargetRepository

logger = logging.getLogger(__name__)


async def build_profile_adapter(
    repo: ProfileRepository,
    profile_id: int,
    channel: str,
    target_id: str = "",
    *,
    store=None,
) -> ChannelAdapter | None:
    """Собрать адаптер канала под профиль+ЦЕЛЬ. None — нет учётки/цели.

    target_id — id ChannelTarget (мультицели). Пусто "" → legacy-путь: цель берётся
    из единственной старой CredentialKind.TARGET (браузер) / telegram-блоба. Задан →
    цель резолвится через ChannelTargetRepository (изоляция по profile_id внутри).
    Сессия/учётка — ОДНА на канал (один аккаунт → много целей).
    """
    validator = VALIDATORS.get(channel)
    if validator is None or not validator.enabled:
        return None

    if store is None:
        store = CheckpointStore(repo.session, profile_id=profile_id)
    session_key = validator.session_channel or channel
    cred = await repo.get_credential(profile_id, session_key, validator.credential_kind)
    if not cred:
        return None  # нет сессии/учётки → сервис отдаст needs_relogin

    if channel == "telegram":
        # Цель расцеплена с сессией: явная цель из выбранной ChannelTarget, блоб —
        # только сид первой цели и фолбэк для legacy-вызова без target_id (Блок 6).
        target, topic_id, peer_access_hash = await _resolve_telegram_target(
            repo, profile_id, target_id
        )
        return await _build_telegram(
            cred,
            store,
            explicit_target=target,
            topic_id=topic_id,
            peer_access_hash=peer_access_hash,
        )

    if channel == "telegraph":
        from crosspost.adapters.api.telegraph import TelegraphAdapter  # noqa: PLC0415

        return TelegraphAdapter(cred, store)  # cred — raw access_token, цель не нужна

    if channel == "instagram":
        # cred — JSON-блоб {access_token, ig_user_id}; цель (ig_user_id) внутри учётки.
        from crosspost.adapters.api.instagram import (  # noqa: PLC0415
            DEFAULT_GRAPH_VERSION,
            InstagramAdapter,
            InstagramError,
            parse_instagram_blob,
        )

        try:
            token, ig_user_id = parse_instagram_blob(cred)
        except InstagramError as exc:
            logger.warning("instagram: %s", exc)
            return None  # неполная учётка → сервис отдаст needs_relogin
        interval = float(os.environ.get("INSTAGRAM_POLL_SECONDS", "30"))
        timeout = float(os.environ.get("INSTAGRAM_POLL_TIMEOUT_SECONDS", "300"))
        ver = os.environ.get("INSTAGRAM_GRAPH_VERSION") or DEFAULT_GRAPH_VERSION
        return InstagramAdapter(
            token,
            ig_user_id,
            store,
            graph_version=ver,
            poll_interval_seconds=interval,
            poll_timeout_seconds=timeout,
        )

    # per-profile ЦЕЛЬ постинга: своя у каждого профиля (изоляция клиентов).
    target = ""
    if validator.needs_target:
        target = await _resolve_target(repo, profile_id, channel, target_id)
        if not target:
            logger.warning("channel %s: не задана цель постинга (target_id=%r)", channel, target_id)
            return None  # без цели не публикуем (иначе постили бы в чужую группу)
        # Нормализуем ПРИ ИСПОЛЬЗОВАНИИ: даже старое «грязное» значение (полный URL)
        # обрежется до screen_name/id — не постим в vk.com/https://vk.com/...
        target = normalize_target(channel, target)

    return _build_browser(channel, cred, store, target)


async def _resolve_target(
    repo: ProfileRepository, profile_id: int, channel: str, target_id: str
) -> str:
    """Значение цели (screen_name/org_id/…) по id, либо legacy-одиночная цель."""
    if target_id:
        trepo = ChannelTargetRepository(repo.session, vault=repo.vault, profile_id=profile_id)
        try:
            tgt = await trepo.get_target(int(target_id))
        except (ValueError, TypeError):
            return ""
        return trepo.decrypt_value(tgt) if tgt is not None else ""
    return await repo.get_credential(profile_id, channel, CredentialKind.TARGET) or ""


async def _resolve_telegram_target(
    repo: ProfileRepository, profile_id: int, target_id: str
) -> tuple[str, int | None, int | None]:
    """Resolve target value + topic_id + peer_access_hash for Telegram.

    Returns (target_value, topic_id, peer_access_hash).
    Legacy path (no target_id): topic_id=None, peer_access_hash=None.
    """
    if target_id:
        trepo = ChannelTargetRepository(repo.session, vault=repo.vault, profile_id=profile_id)
        try:
            tgt = await trepo.get_target(int(target_id))
        except (ValueError, TypeError):
            return "", None, None
        if tgt is None:
            return "", None, None
        return trepo.decrypt_value(tgt), tgt.topic_id, tgt.peer_access_hash
    value = await repo.get_credential(profile_id, "telegram", CredentialKind.TARGET) or ""
    return value, None, None


async def _build_telegram(
    cred: str,
    store,
    *,
    explicit_target: str = "",
    topic_id: int | None = None,
    peer_access_hash: int | None = None,
) -> ChannelAdapter | None:
    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    from crosspost.adapters.api.telegram import TelegramAdapter  # noqa: PLC0415

    cfg = parse_credential_blob(cred)
    api_id = int(cfg.get("api_id") or 0)
    api_hash = str(cfg.get("api_hash") or "")
    session = str(cfg.get("session") or "")
    # Явная цель (выбранная ChannelTarget) приоритетна; блоб — фолбэк (legacy/сид).
    target = explicit_target or str(cfg.get("target_channel") or "")
    if not (api_id and api_hash and session and target):
        logger.warning("telegram: неполная учётка профиля")
        return None

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()  # сессия уже авторизована — без интерактива
    return TelegramAdapter(
        client, target=target, store=store, topic_id=topic_id, peer_access_hash=peer_access_hash
    )


def _build_browser(channel: str, storage_state: str, store, target: str) -> ChannelAdapter | None:
    """target — per-profile цель (screen_name / org_id / channel_id). НЕ из env."""
    headless = browser_headless()  # публикация уважает флаг; логин — всегда headful

    if channel == "yandex":
        from crosspost.adapters.browser.yandex import YandexBrowserAdapter  # noqa: PLC0415

        return YandexBrowserAdapter(
            target,
            store,
            headless=headless,
            storage_state=storage_state,
            proxy_url=os.environ.get("YANDEX_PROXY_URL", ""),
        )
    if channel == "vk_wall":
        from crosspost.adapters.browser.vk_wall import VKWallBrowserAdapter  # noqa: PLC0415

        return VKWallBrowserAdapter(
            target,
            store,
            headless=headless,
            storage_state=storage_state,
            proxy_url=os.environ.get("VK_PROXY_URL", ""),
        )
    if channel == "vk_channel":
        from crosspost.adapters.browser.vk_channel import VKChannelBrowserAdapter  # noqa: PLC0415

        return VKChannelBrowserAdapter(
            target,
            store,
            headless=headless,
            storage_state=storage_state,
            proxy_url=os.environ.get("VK_PROXY_URL", ""),
        )
    return None
