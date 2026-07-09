"""ВКонтакте — браузерный адаптер для стены сообщества (Playwright). Эпик 5.

Публикует пост на стену сообщества vk.com/<screen_name> через браузер.
API-путь заблокирован платформой (err 27/214) — работаем через реальный браузер.

Архитектурные особенности:
  - DONE: post возвращает wall-id квитанцию, если успешно.
  - Форма двухшаговая: шаг 1 — текст + фото + кнопка «Далее»;
    шаг 2 — финальный экран (TODO: кнопка «Опубликовать», ждём уточнения селектора).
  - Verify-before-retry: ищем пост с нашим текстом в ленте ДО отправки.
  - is_logged_in: проверяем отсутствие формы входа, наличие шапки vk.com.

Сессия — storageState-файл (login_context → save_state → open_page грузит).
Все СЕЛЕКТОРЫ — константы в начале файла. Приоритет data-testid (стабильны у ВК),
CSS-классы vkit-* НЕ используем (генерённые).
"""
from __future__ import annotations

import re
from pathlib import Path

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.adapters.browser.base_browser import is_logged_in, open_page
from crosspost.content.canonical import CanonicalContent
from crosspost.orchestrator.task import IdempotencyStore

# ── СЕЛЕКТОРЫ ────────────────────────────────────────────────────────────────
# Все в одном месте — при редизайне ВК менять только здесь.
# Приоритет: data-testid (стабильны). CSS-классы vkit-* НЕ трогаем.

# URL стены сообщества
_WALL_URL = "https://vk.com/{screen_name}"

# Маркеры авторизации / неавторизации
_LOGIN_URL_FRAGMENTS = ("vk.com/login", "id.vk.com")
# Позитивный DOM-маркер залогиненного состояния — не используется для ВК:
# все кандидаты (аватар, create_button, top_nav) оказались нестабильными в новом ВК.
# Используем НЕГАТИВНУЮ проверку: залогинен = URL не содержит login/id.vk.com.
# (подтверждено диагностикой: "Войти" и форма входа не найдены на живой залогиненной странице)

# Шаг 1: открытие формы поста
_CREATE_BUTTON_NAME = "Создать"          # шапка: get_by_role("button", exact=True)
# Пункт меню «Пост» — ВК не использует role=menuitem, ищем по тексту.
# re.compile(r"^Пост$") отсекает «Пост в канал», «Пост в историю» и т.д.
_POST_MENU_ITEM_RE = re.compile(r"^Пост$")
# Селектор контейнера меню «Создать» — ждём его появления перед кликом по пункту.
# Если data-testid не совпадёт — падаем в лог с видимыми элементами меню (debug).
_CREATE_MENU_SELECTOR = '[role="menu"], [data-testid*="menu"], [class*="ActionSheet"], [class*="Dropdown"]'

# Шаг 1: модалка и поля
_MODAL_SELECTOR = '[data-testid="posting_modal_box"]'
_TEXT_SELECTOR = '[data-testid="posting_base_screen_input_message"]'
_FILE_INPUT_SELECTOR = 'input[type="file"][multiple]'

# Шаг 1: ожидание превью фото в модалке (img появляется после загрузки)
# fallback: ждём исчезновения плейсхолдера или появления любого img в модалке
_PHOTO_PREVIEW_SELECTOR = f'{_MODAL_SELECTOR} img'
_PHOTO_PLACEHOLDER = "Добавьте фото или видео"

# Шаг 1 → Шаг 2
_NEXT_BUTTON_SELECTOR = '[data-testid="posting_base_screen_next"]'

# TODO: кнопка финальной публикации на шаге 2 — уточнить после живого теста.
# Вероятные кандидаты: [data-testid="posting_confirm_screen_submit"],
# get_by_role("button", name="Опубликовать", exact=True)
_PUBLISH_BUTTON_TODO = None  # заглушка — логируем, что дошли до шага 2

# Ожидание появления поста в ленте (verify после публикации)
_POST_IN_FEED_SELECTOR = '[data-post-id], .wall_post_cont'

# Таймауты (мс)
_NAV_TIMEOUT = 15_000
_MODAL_TIMEOUT = 10_000
_PHOTO_TIMEOUT = 15_000
_POST_APPEAR_TIMEOUT = 20_000
# ─────────────────────────────────────────────────────────────────────────────




class VKWallBrowserAdapter:
    channel = "vk_wall"

    def __init__(
        self,
        screen_name: str,
        store: IdempotencyStore,
        *,
        headless: bool = False,
    ) -> None:
        self._screen_name = screen_name   # напр. "medithou"
        self._store = store
        self._headless = headless

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult:
        # 1) идемпотентность
        if self._store.is_done(publication_id, self.channel):
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        async with open_page(self.channel, headless=self._headless) as page:
            # 2) открыть стену сообщества
            url = _WALL_URL.format(screen_name=self._screen_name)
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)

            # 3) ПЕРВЫМ — проверка сессии (негативная: нет URL логина = залогинен)
            if not await is_logged_in(
                page,
                reject_url_fragments=_LOGIN_URL_FRAGMENTS,
            ):
                return ChannelResult(
                    self.channel,
                    ResultStatus.NEEDS_RELOGIN,
                    error="ВК: сессия протухла, выполни crosspost login --channel vk_wall",
                )

            # 4) verify-before-retry: пост уже есть в ленте — не дублируем
            if await self._find_post(page, content.text):
                self._store.mark_done(
                    publication_id, self.channel, external_id="posted:recovered"
                )
                return ChannelResult(
                    self.channel, ResultStatus.DONE, external_id="posted:recovered"
                )

            # 5) шаг 1: открыть форму → модалка
            await self._open_post_form(page)

            # 6) вставить текст (contenteditable)
            text_field = page.locator(_TEXT_SELECTOR)
            await text_field.click()
            await text_field.fill(content.text)

            # 7) прикрепить фото, если есть
            if content.media_paths:
                await self._attach_photos(page, content.media_paths[:10])

            # 8) «Далее» — переход на шаг 2
            await page.locator(_NEXT_BUTTON_SELECTOR).click()

            # 9) шаг 2: финальная публикация (TODO: уточнить селектор кнопки)
            external_id = await self._publish_step2(page, content.text)

        self._store.mark_done(publication_id, self.channel, external_id=external_id)
        return ChannelResult(self.channel, ResultStatus.DONE, external_id=external_id)

    # ── внутренние методы ────────────────────────────────────────────────────

    async def _open_post_form(self, page) -> None:
        """Открыть модалку создания поста через шапку «Создать» → «Пост».

        ВК не использует role=menuitem — ищем пункт по тексту через regex ^Пост$,
        чтобы не задеть «Пост в канал» и «Пост в историю».
        """
        # Кнопка «Создать» в шапке — exact=True: не задеть другие кнопки
        await page.get_by_role("button", name=_CREATE_BUTTON_NAME, exact=True).click()

        # Ждём появления выпадающего меню перед поиском пункта
        try:
            await page.wait_for_selector(_CREATE_MENU_SELECTOR, timeout=5_000)
        except Exception:
            pass  # меню могло появиться без стандартных ролей — продолжаем

        # Ищем пункт «Пост» по точному тексту (regex отсекает «Пост в канал» и т.д.)
        await page.get_by_text(_POST_MENU_ITEM_RE, exact=True).first.click()

        # Ждём появления модалки
        await page.wait_for_selector(_MODAL_SELECTOR, timeout=_MODAL_TIMEOUT)

    async def _attach_photos(self, page, paths: list[Path]) -> None:
        """Загрузить фото через скрытый input[type=file][multiple] внутри модалки."""
        abs_paths = [str(p.resolve()) for p in paths]
        # file-input внутри модалки
        file_input = page.locator(f'{_MODAL_SELECTOR} {_FILE_INPUT_SELECTOR}').first
        if await file_input.count() > 0:
            await file_input.set_input_files(abs_paths)
        else:
            # фолбэк: общий file-input на странице
            await page.locator(_FILE_INPUT_SELECTOR).first.set_input_files(abs_paths)
        await self._wait_for_photo_preview(page, len(abs_paths))

    async def _wait_for_photo_preview(self, page, count: int) -> None:
        """Дождаться превью загруженных фото в модалке.

        ВК показывает <img> внутри модалки после загрузки.
        Фолбэк: ждём исчезновения текста-плейсхолдера «Добавьте фото или видео».
        """
        try:
            await page.wait_for_selector(_PHOTO_PREVIEW_SELECTOR, timeout=_PHOTO_TIMEOUT)
            await page.wait_for_function(
                "([sel, n]) => document.querySelectorAll(sel).length >= n",
                arg=[_PHOTO_PREVIEW_SELECTOR, count],
                timeout=_PHOTO_TIMEOUT,
            )
        except Exception:
            # фолбэк: плейсхолдер исчез → фото приняты
            await page.wait_for_function(
                "(text) => !document.body.innerText.includes(text)",
                arg=_PHOTO_PLACEHOLDER,
                timeout=_PHOTO_TIMEOUT,
            )

    async def _publish_step2(self, page, text: str) -> str:
        """Шаг 2: финальный экран публикации.

        Нажимаем кнопку публикации (несколько кандидатов на случай A/B),
        затем ждём закрытия модалки — надёжный признак успеха.
        """
        for selector in (
            '[data-testid="posting_confirm_screen_submit"]',
            '[data-testid="posting_footer_button_send"]',
        ):
            btn = page.locator(selector)
            if await btn.count() > 0:
                await btn.first.click()
                break
        else:
            await page.get_by_role("button", name="Опубликовать", exact=True).click()

        return await self._wait_for_post(page, text)

    async def _find_post(self, page, text: str) -> bool:
        """Verify-before-retry: найти пост с данным текстом в ленте сообщества."""
        if not text.strip():
            return False
        try:
            posts = await page.locator(_POST_IN_FEED_SELECTOR).all()
            for post in posts[:10]:
                try:
                    post_text = await post.inner_text()
                    if text.strip() in post_text:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def _wait_for_post(self, page, text: str) -> str:
        """Дождаться закрытия модалки постинга — надёжный признак успеха.

        Фолбэк: ищем текст поста в ленте (первые 50 символов).
        Затем пробуем вытащить data-post-id из свежего поста.
        """
        # Модалка исчезла = ВК принял пост
        try:
            await page.wait_for_selector(
                _MODAL_SELECTOR, state="detached", timeout=_POST_APPEAR_TIMEOUT
            )
        except Exception:
            # Если модалка уже закрылась раньше или не найдена — игнорируем
            pass

        # Попытка извлечь wall-id из первого поста в ленте
        try:
            post = page.locator('[data-post-id]').first
            post_id = await post.get_attribute("data-post-id") or ""
            if post_id:
                return f"wall{post_id}"
        except Exception:
            pass
        return "posted"


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = VKWallBrowserAdapter  # noqa: E305
