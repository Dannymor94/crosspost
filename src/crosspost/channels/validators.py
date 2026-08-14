"""Реестр валидаторов подключений. Слой 0.4.

Каждый канал объявлен один раз через ChannelValidatorDef.
validate_connection() читает реестр — никаких if/elif по каналам в общем коде.

Два вида:
  "api"     — берёт plaintext-учётку из vault, делает лёгкий тестовый запрос.
  "browser" — открывает страницу через open_page, вызывает is_logged_in.

Вызывать по требованию (при сохранении учётки или из UI). Фоновый health-check — Слой 2.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from crosspost.channels.guide import (
    TELEGRAM_GUIDE,
    TELEGRAPH_GUIDE,
    VK_CHANNEL_GUIDE,
    VK_WALL_GUIDE,
    YANDEX_GUIDE,
    ChannelGuide,
)
from crosspost.db.models import CredentialKind

logger = logging.getLogger(__name__)

# Тип валидирующей функции: принимает credential (plaintext или None для browser)
# и возвращает True если соединение живое.
ValidatorFn = Callable[[str | None], Coroutine[Any, Any, bool]]

# Нормализатор цели: сырой ввод пользователя → чистое значение (screen_name/id).
# Бросает ValueError с человеческим сообщением, если ввод не распознан (верификация).
TargetNormalizer = Callable[[str], str]


class TargetError(ValueError):
    """Ввод цели постинга не распознан (для 422 с человеческим текстом)."""


def normalize_vk_screen_name(raw: str) -> str:
    """Любой формат адреса группы VK → короткое имя.

    'https://vk.com/yogapiecom' | 'vk.com/yogapiecom' | '@yogapiecom' | 'yogapiecom'
    → 'yogapiecom'. Числовой 'club123' / 'public123' — оставляем как есть.
    """
    s = raw.strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^(m\.|www\.)?vk\.com/", "", s, flags=re.I)
    s = s.lstrip("@").strip("/")
    s = s.split("/")[0].split("?")[0].strip()  # первый сегмент пути, без query
    # VK screen name: буквы/цифры/подчёркивание (точки/домены недопустимы).
    if not re.fullmatch(r"[A-Za-z0-9_]+", s or ""):
        raise TargetError(
            "Не похоже на адрес группы VK. Введите короткое имя (vk.com/XXXX → XXXX)."
        )
    return s


def normalize_vk_channel_id(raw: str) -> str:
    """ID канала VK (числовой, обычно отрицательный) из строки/URL."""
    m = re.search(r"-?\d+", raw or "")
    if not m:
        raise TargetError("Укажите числовой id канала VK (напр. -240033402).")
    return m.group(0)


def normalize_yandex_org(raw: str) -> str:
    """ID организации Яндекс Бизнес: из URL yandex.ru/sprav/<id>/… или голого числа."""
    s = raw or ""
    m = re.search(r"/sprav/(\d+)", s) or re.search(r"\b(\d{3,})\b", s)
    if not m:
        raise TargetError("Укажите числовой ID организации (yandex.ru/sprav/<ID>/…).")
    return m.group(1)


def normalize_telegram_target(raw: str) -> str:
    """Канал назначения Telegram → @username или числовой id.

    '@my_channel' | 'my_channel' | 'https://t.me/my_channel' | 't.me/my_channel'
    → '@my_channel'. Числовой id (-100…) остаётся как есть (телетон принимает оба).
    """
    s = (raw or "").strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^t\.me/", "", s, flags=re.I)
    s = s.strip("/").split("/")[0].split("?")[0].strip()
    if not s:
        raise TargetError("Укажите канал: @username или ссылку t.me/…")
    if re.fullmatch(r"-?\d+", s):  # числовой id канала — как есть
        return s
    handle = s.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]+", handle):
        raise TargetError("Не похоже на канал Telegram. Укажите @username или t.me/…")
    return "@" + handle


@dataclass(frozen=True)
class ChannelValidatorDef:
    """Декларация валидатора для одного канала."""

    kind: Literal["api", "browser"]
    credential_kind: CredentialKind  # какую учётку брать из vault
    fn: ValidatorFn  # async (credential: str | None) -> bool
    title: str = ""  # человеческое название для UI
    enabled: bool = True  # False — не показывать в UI (канал отложен/не работает)
    interactive: bool = False  # True — вход многошаговый в UI (Telegram: телефон+код+2FA)
    # ── Поля браузерного логина (kind="browser") ──────────────────────────────
    login_url: str = ""  # стартовая страница входа (пользователь логинится руками)
    probe_url: str = ""  # ЗАЩИЩЁННАЯ страница: аноним → редирект на логин; вошёл → остаётся.
    # На неё confirm навигирует для ПОЗИТИВНОЙ проверки входа (не по URL стартовой).
    reject_fragments: tuple[str, ...] = ()  # фрагменты URL = «ещё не залогинен»
    logged_out_selectors: tuple[str, ...] = ()  # DOM-маркеры НЕзалогиненного
    # (форма входа / кнопка «Войти» / заголовок «Добро пожаловать») → present = не вошёл
    require_selector: str | None = None  # DOM-маркер кабинета/залогиненного (опц.)
    session_cookie_domains: tuple[str, ...] = ()  # снятый storageState ДОЛЖЕН иметь
    # куки этих доменов — иначе сессия пустая → «вход не выполнен» (страховка от ложного LIVE)
    session_channel: str | None = None  # из какого канала брать/писать сессию
    # (vk_channel делит аккаунт с vk_wall → session_channel="vk_wall")
    # ── Цель постинга (per-profile) ───────────────────────────────────────────
    # Сессия может быть общей (один аккаунт), но КУДА постим — своё у каждого
    # профиля. Без цели канал не eligible: разные клиенты → разные группы.
    needs_target: bool = False
    target_label: str = ""  # подпись поля цели в UI
    target_hint: str = ""  # подсказка «что вводить»
    target_normalizer: TargetNormalizer | None = None  # сырой ввод → чистое значение
    # ── Инструкция подключения (декларация, не хардкод в UI) ───────────────────
    guide: ChannelGuide | None = None  # пошаговая инструкция для клиента-нетехнаря


# ── Валидирующие функции ──────────────────────────────────────────────────────


async def _validate_telegram(token: str | None) -> bool:
    """Telethon: проверяем строку сессии через get_me().

    Учётка из UI — JSON {api_id, api_hash, target_channel, session}: берём
    per-profile api_id/api_hash. Голая строка сессии (легаси/CLI) — fallback
    на api_id/api_hash из env.
    """
    if not token:
        return False
    try:
        import os

        from telethon import TelegramClient
        from telethon.sessions import StringSession

        from crosspost.channels.telegram_login import parse_credential_blob

        session_str = token
        api_id = 0
        api_hash = ""
        cfg = parse_credential_blob(token)
        if cfg.get("session"):
            session_str = str(cfg["session"])
            api_id = int(cfg.get("api_id") or 0)
            api_hash = str(cfg.get("api_hash") or "")
        if not api_id or not api_hash:
            api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
            api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        if not api_id or not api_hash:
            logger.warning("api_id/api_hash не заданы — пропускаем проверку telegram")
            return False
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await client.connect()
        me = await client.get_me()
        await client.disconnect()
        return me is not None
    except Exception as exc:
        logger.debug("telegram validate failed: %s", exc)
        return False


async def _validate_telegraph(token: str | None) -> bool:
    """Telegraph: getAccountInfo с access_token.

    API: https://telegra.ph/api#getAccountInfo
    """
    if not token:
        return False
    try:
        import httpx

        resp = await httpx.AsyncClient(timeout=10).get(
            "https://api.telegra.ph/getAccountInfo",
            params={"access_token": token},
        )
        return bool(resp.json().get("ok"))
    except Exception as exc:
        logger.debug("telegraph validate failed: %s", exc)
        return False


async def _validate_vk_api(token: str | None) -> bool:
    """VK API: users.get или groups.getById с токеном."""
    if not token:
        return False
    try:
        import httpx

        resp = await httpx.AsyncClient().get(
            "https://api.vk.com/method/users.get",
            params={"access_token": token, "v": "5.199"},
            timeout=10,
        )
        data = resp.json()
        return "error" not in data
    except Exception as exc:
        logger.debug("vk api validate failed: %s", exc)
        return False


def _make_browser_validator(
    url: str,
    reject_url_fragments: tuple[str, ...],
    require_selector: str | None = None,
    session_channel: str | None = None,
) -> ValidatorFn:
    """Фабрика: возвращает async-функцию, открывающую url и проверяющую is_logged_in."""

    async def _validate(storage_state: str | None) -> bool:
        # storage_state — per-profile сессия из credentials (расшифрована в памяти).
        # Передаём её В open_page явно: НИКАКОГО общего файла — изоляция по профилю.
        # Пустой storage_state → пустой контекст → is_logged_in=False → NEEDS_RELOGIN.
        try:
            from crosspost.adapters.browser.base_browser import is_logged_in, open_page

            channel_key = session_channel or _channel_from_url(url)
            async with open_page(channel_key, headless=True, storage_state=storage_state) as page:
                await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                return await is_logged_in(
                    page,
                    reject_url_fragments=reject_url_fragments,
                    require_selector=require_selector,
                )
        except Exception as exc:
            logger.debug("browser validate failed for %s: %s", url, exc)
            return False

    return _validate


def _channel_from_url(url: str) -> str:
    """Эвристика: достать имя канала из URL (используется только как fallback)."""
    if "yandex" in url:
        return "yandex"
    if "vk.com" in url:
        return "vk_wall"
    return "unknown"


# ── Реестр каналов ────────────────────────────────────────────────────────────

# ПОЧЕМУ детекция входа НЕ по DOM-тексту:
# Негативная проверка «на странице есть слово Войти» ненадёжна — оно встречается
# и на ЗАЛОГИНЕННЫХ страницах ВК (футер, скрытые виджеты) → ложный «не вошёл».
# Позитивные DOM-селекторы кабинета угадывать нельзя (CLAUDE.md), а снять живой
# DOM из кода нельзя. Поэтому вход определяем по НАДЁЖНЫМ фактам:
#   1) probe_url — ЗАЩИЩЁННАЯ страница: аноним → редирект на login (reject_fragments);
#      залогинен → остаётся. URL-проверка на защищённой странице надёжна.
#   2) session_cookie_domains — снятый storageState содержит куки аккаунта.
# require_selector/logged_out_selectors ОСТАВЛЕНЫ пустыми: заполнить ТОЛЬКО
# устойчивым маркером, снятым с ЖИВОЙ страницы (make smoke), тогда добавится
# строгая позитивная DOM-проверка. Пустые = не участвуют (не ломают детекцию).

VALIDATORS: dict[str, ChannelValidatorDef] = {
    "telegraph": ChannelValidatorDef(
        kind="api",
        credential_kind=CredentialKind.API_TOKEN,
        fn=_validate_telegraph,
        title="Telegraph",
        enabled=True,
        interactive=False,
        guide=TELEGRAPH_GUIDE,
    ),
    "telegram": ChannelValidatorDef(
        kind="api",
        credential_kind=CredentialKind.API_TOKEN,
        fn=_validate_telegram,
        title="Telegram",
        enabled=True,
        interactive=True,  # вход по телефону+коду прямо в UI, см. telegram_login
        # Мультицели: одна сессия обслуживает несколько @каналов. Цель расцеплена с
        # сессией — хранится в channel_targets (первая сидится из блоба target_channel).
        needs_target=True,
        target_label="Канал назначения",
        target_hint="@username канала или ссылка t.me/… (вы должны быть админом)",
        target_normalizer=normalize_telegram_target,
        guide=TELEGRAM_GUIDE,
    ),
    "vk": ChannelValidatorDef(
        kind="api",
        credential_kind=CredentialKind.API_TOKEN,
        fn=_validate_vk_api,
        title="ВКонтакте (API)",
        enabled=False,  # VK блокирует токен сообщества; ждёт user-токена — см. PRD_BACKLOG
    ),
    "yandex": ChannelValidatorDef(
        kind="browser",
        credential_kind=CredentialKind.STORAGE_STATE,
        fn=_make_browser_validator(
            url="https://yandex.ru/business",
            reject_url_fragments=("passport.yandex", "auth/login", "accounts/login"),
            # require_selector НЕ задаём: прежний ".YandexBusinessCabinet" был угадан
            # и давал ложный «не вошёл». Полагаемся на URL-редирект + куки.
        ),
        title="Яндекс Бизнес",
        enabled=True,
        login_url="https://yandex.ru/sprav/",
        probe_url="https://yandex.ru/business",
        reject_fragments=("passport.yandex", "auth/login", "accounts/login"),
        # require_selector: снять с живой yandex.ru/business (маркер кабинета) и вписать.
        session_cookie_domains=("yandex.ru", "yandex.com"),
        needs_target=True,
        target_label="ID организации",
        target_hint="Из URL кабинета: yandex.ru/sprav/<ЭТОТ-ID>/…",
        target_normalizer=normalize_yandex_org,
        guide=YANDEX_GUIDE,
    ),
    "vk_wall": ChannelValidatorDef(
        kind="browser",
        credential_kind=CredentialKind.STORAGE_STATE,
        fn=_make_browser_validator(
            url="https://vk.com/feed",
            reject_url_fragments=("vk.com/login", "id.vk.com"),
        ),
        title="ВКонтакте · стена",
        enabled=True,
        login_url="https://vk.com/",
        probe_url="https://vk.com/feed",
        reject_fragments=("vk.com/login", "id.vk.com"),
        # require_selector: снять с живой vk.com/feed (аватар/ссылка на свою страницу).
        session_cookie_domains=("vk.com",),
        needs_target=True,
        target_label="Адрес группы",
        target_hint="Вставьте адрес группы: vk.com/XXXX или полную ссылку — оставим только имя",
        target_normalizer=normalize_vk_screen_name,
        guide=VK_WALL_GUIDE,
    ),
    "vk_channel": ChannelValidatorDef(
        kind="browser",
        credential_kind=CredentialKind.STORAGE_STATE,
        fn=_make_browser_validator(
            url="https://vk.com/feed",
            reject_url_fragments=("vk.com/login", "id.vk.com"),
            session_channel="vk_wall",  # vk_channel использует сессию vk_wall
        ),
        title="ВКонтакте · канал",
        enabled=True,
        login_url="https://vk.com/",
        probe_url="https://vk.com/feed",
        reject_fragments=("vk.com/login", "id.vk.com"),
        # require_selector: снять с живой vk.com/feed (аватар/ссылка на свою страницу).
        session_cookie_domains=("vk.com",),
        session_channel="vk_wall",  # сессию берём/пишем в vk_wall (общий аккаунт)
        needs_target=True,
        target_label="ID канала VK",
        target_hint="Числовой id канала сообщества (напр. -240033402)",
        target_normalizer=normalize_vk_channel_id,
        guide=VK_CHANNEL_GUIDE,
    ),
}


def normalize_target(channel: str, raw: str) -> str:
    """Применить нормализатор канала к цели (безопасно, для использования/показа).

    Делает результат корректным ДАЖЕ для значений, сохранённых до нормализации
    (напр. полный URL). Если нормализатор не задан или ввод не распознан —
    возвращаем как есть (не роняем публикацию из-за старого значения).
    """
    v = VALIDATORS.get(channel)
    if v is None or v.target_normalizer is None or not raw:
        return raw
    try:
        return v.target_normalizer(raw)
    except ValueError:
        return raw
