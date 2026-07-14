"""Интерактивный вход в браузерные каналы (VK/Яндекс) из UI. Итерация 1 / Слой 1.

Двухшаговый флоу, как у Telegram — но НИКОГДА не решаем «вошёл» по URL стартовой
страницы (это давало ложный успех: у ВК аноним на vk.com/ не редиректит на /login):

    begin(profile_id, channel)    # открыть СВЕЖИЙ ПУСТОЙ контекст, показать логин
    confirm(profile_id, channel)  # пользователь нажал «Я вошёл» → ПОЗИТИВНАЯ проверка
    cancel(profile_id, channel)   # закрыть окно, ничего не сохранять

Гарантии изоляции:
  - контекст входа СВЕЖИЙ и ПУСТОЙ (browser.new_context() без storage_state) —
    никакие куки прошлых сессий/других профилей не подхватываются;
  - вход подтверждается ПОЛЬЗОВАТЕЛЕМ, а не эвристикой; сервер лишь ПРОВЕРЯЕТ его
    навигацией на probe_url (защищённая страница: аноним → редирект на логин);
  - storageState снимается ТОЛЬКО после успешной проверки и кладётся в credentials
    ЭТОГО профиля (шифрование vault — на уровне роута).

Живой браузер держится в памяти сервера между запросами (_SESSIONS), как
telegram_login. Один процесс-владелец (uvicorn, 1 воркер) — то же допущение.

Тестируется на моках Playwright: подменяется _launch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from crosspost.channels.validators import VALIDATORS

logger = logging.getLogger(__name__)


class BrowserLoginError(Exception):
    """Ошибка флоу с человекочитаемым сообщением (показывается в UI)."""


@dataclass
class _LiveLogin:
    """Живое окно входа. Держится в памяти между begin и confirm."""

    profile_id: int
    channel: str
    session_key: str
    pw: Any
    browser: Any
    context: Any
    page: Any


# (profile_id, channel) → активное окно входа.
_SESSIONS: dict[tuple[int, str], _LiveLogin] = {}


async def _launch(headless: bool = False) -> tuple[Any, Any, Any, Any]:
    """Поднять браузер и СВЕЖИЙ ПУСТОЙ контекст. Подменяется в тестах.

    Пустой контекст = никаких кук: пользователь обязан войти руками.
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=headless)
    context = await browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
    page = await context.new_page()
    return pw, browser, context, page


async def _close(sess: _LiveLogin) -> None:
    """Закрыть контекст/браузер/playwright — best-effort."""
    for closer in (sess.context.close, sess.browser.close, sess.pw.stop):
        try:
            await closer()
        except Exception as exc:  # noqa: BLE001
            logger.debug("browser login close failed: %s", exc)


def _validator(channel: str):
    v = VALIDATORS.get(channel)
    if v is None:
        raise BrowserLoginError(f"Канал '{channel}' не в реестре.")
    if v.kind != "browser":
        raise BrowserLoginError(f"Канал '{channel}' — не браузерный.")
    return v


async def begin(profile_id: int, channel: str, *, headless: bool = False) -> str:
    """Открыть окно входа (свежий пустой контекст) на странице логина канала."""
    v = _validator(channel)
    await cancel(profile_id, channel)  # закрыть возможное недоделанное окно

    pw, browser, context, page = await _launch(headless=headless)
    session_key = v.session_channel or channel
    _SESSIONS[(profile_id, channel)] = _LiveLogin(
        profile_id=profile_id,
        channel=channel,
        session_key=session_key,
        pw=pw,
        browser=browser,
        context=context,
        page=page,
    )
    try:
        await page.goto(v.login_url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:  # noqa: BLE001 — окно оставляем открытым даже при флаки-goto
        logger.debug("login goto failed for %s: %s", channel, exc)
    return session_key


async def confirm(profile_id: int, channel: str) -> dict | None:
    """Проверить вход ПОЗИТИВНО (навигация на probe_url).

    Возвращает storageState (dict) при успехе — окно закрывается.
    None — пользователь ещё НЕ вошёл; окно остаётся открытым для повторной попытки.
    """
    sess = _SESSIONS.get((profile_id, channel))
    if sess is None:
        raise BrowserLoginError("Окно входа не открыто — нажмите «Войти» заново.")

    v = _validator(channel)
    from crosspost.adapters.browser.base_browser import is_logged_in

    try:
        await sess.page.goto(v.probe_url, wait_until="domcontentloaded", timeout=30_000)
        # (1) Дождаться РЕАЛЬНОЙ загрузки — иначе проверяем пустой DOM и ложно «вошли».
        await _settle(sess.page)
        # (2) Проверка по ФАКТУ (URL + DOM-маркеры), а не только по URL.
        ok = await is_logged_in(
            sess.page,
            reject_url_fragments=v.reject_fragments,
            reject_selectors=v.logged_out_selectors,
            require_selector=v.require_selector,
        )
    except Exception as exc:
        raise BrowserLoginError(f"Не удалось проверить вход: {exc}") from exc

    if not ok:
        return None  # ещё не вошёл — держим окно

    state = await sess.context.storage_state()
    # (3) Страховка: снятая сессия должна содержать куки домена. Пустая → НЕ успех.
    if not _has_session_cookies(state, v.session_cookie_domains):
        raise BrowserLoginError(
            "Вход не выполнен: сессия пустая (нет кук аккаунта). "
            "Войдите в аккаунт до рабочего кабинета и повторите."
        )

    await _close(sess)
    _SESSIONS.pop((profile_id, channel), None)
    return state


async def _settle(page) -> None:
    """Дождаться, пока страница реально загрузится (сеть утихнет).

    networkidle может не наступить на «живых» SPA — тогда просто идём дальше:
    к этому моменту DOM уже отрендерен (domcontentloaded + попытка networkidle).
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("networkidle wait skipped: %s", exc)


def _has_session_cookies(state: dict, domains: tuple[str, ...]) -> bool:
    """В snapshot'е storageState есть хотя бы одна кука одного из доменов?

    Пустой storageState (баг «сохранили пустую сессию») → False. Если домены
    не заданы для канала — проверка не требуется (True).
    """
    if not domains:
        return True
    cookies = state.get("cookies", []) if isinstance(state, dict) else []
    for c in cookies:
        dom = str(c.get("domain", "")).lstrip(".")
        if any(d in dom for d in domains):
            return True
    return False


async def cancel(profile_id: int, channel: str) -> None:
    """Закрыть окно входа профиля, если оно есть. Ничего не сохраняем."""
    sess = _SESSIONS.pop((profile_id, channel), None)
    if sess is not None:
        await _close(sess)
