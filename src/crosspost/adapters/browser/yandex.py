"""Яндекс Бизнес — браузерный адаптер (Playwright). Эпик 5.

Раздел «Публикации» (yandex.ru/sprav/<org_id>/posts или business.yandex.ru).
Публикация уходит на модерацию (до 7 дней) — возвращаем SUBMITTED, не DONE.

Архитектурные особенности:
  - FIRE-AND-FORGET: publish() доводит до нажатия «Создать» и возвращает SUBMITTED.
    Финал модерации НЕ отслеживаем (post-MVP).
  - Медиа чистить СРАЗУ после SUBMITTED: файл уже в Яндексе, ждать нечего.
    Сигнал для media lifecycle — статус SUBMITTED (эпик 7, post-MVP).
  - При отсутствии авторизации (видим passport/login) → NEEDS_RELOGIN,
    не путаем с ошибкой публикации.

Verify-before-retry (CLAUDE.md инвариант 2):
  Перед отправкой ищем карточку с нашим текстом в списке ниже формы.
  Если нашли — возвращаем SUBMITTED без повторной отправки.

Все СЕЛЕКТОРЫ — константы в начале файла. По тексту/роли/placeholder,
НЕ по CSS-классам (они генерённые у Яндекса).
"""
from __future__ import annotations

from pathlib import Path

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.adapters.browser.base_browser import is_logged_in, open_page
from crosspost.content.canonical import CanonicalContent
from crosspost.orchestrator.task import IdempotencyStore

# ── СЕЛЕКТОРЫ ────────────────────────────────────────────────────────────────
# Все в одном месте — при редизайне Яндекса менять только здесь.

# URL
_POSTS_URL = "https://yandex.ru/sprav/{org_id}/p/edit/posts/"
_STORIES_URL = "https://yandex.ru/sprav/{org_id}/p/edit/stories/"  # для type=story (post-MVP)

# Маркеры авторизации / неавторизации
_LOGIN_URL_FRAGMENTS = ("passport.yandex", "auth/login", "accounts/login")
_CABINET_SELECTOR = 'a[href*="/sprav/"], [data-testid*="org"], nav'

# Форма публикации
_TEXT_PLACEHOLDER = "Расскажите о событиях, акциях и новостях"
_CREATE_BUTTON_NAME = "Создать"
_PHOTO_BUTTON_NAME = "Добавить"
# Кнопка публикации поста. На странице несколько кнопок «Создать…»
# («Создать событие», «Создать историю») — частичный матч по имени ловит все три
# (strict mode violation). Берём ровно нашу: семантический класс, а фолбэком —
# точное имя (exact=True отсекает «событие»/«историю»).
_SUBMIT_BUTTON_SELECTOR = "button.PostAddForm-Submit"

# Превью загруженного фото. Яндекс рисует его как DIV с background-image
# (avatars.mds.yandex.net/...), НЕ как <img src="blob:...">.
#   контейнер:  div.PostAddForm-Photos (он же PostPhotosCollection)
#   одно фото:  div.PostPhotosCollection-Photo (style: background-image: url(...))
_PHOTO_PREVIEW_SELECTOR = ".PostPhotosCollection-Photo"

# Карточки публикаций (список ниже формы)
_CARD_TEXT_SELECTOR = "article, [class*='post'], [class*='card'], [class*='item']"

# Таймауты (мс)
_NAV_TIMEOUT = 15_000
_CARD_APPEAR_TIMEOUT = 20_000
_FILE_UPLOAD_TIMEOUT = 10_000
# ─────────────────────────────────────────────────────────────────────────────


class YandexBrowserAdapter:
    channel = "yandex"

    def __init__(
        self,
        org_id: str,
        store: IdempotencyStore,
        *,
        headless: bool = False,
    ) -> None:
        self._org_id = org_id
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
            # 2) переходим в раздел Публикации
            url = _POSTS_URL.format(org_id=self._org_id)
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)

            # 3) ПЕРВЫМ — проверка сессии
            if not await is_logged_in(
                page,
                reject_url_fragments=_LOGIN_URL_FRAGMENTS,
                require_selector=_CABINET_SELECTOR,
            ):
                return ChannelResult(
                    self.channel,
                    ResultStatus.NEEDS_RELOGIN,
                    error="Яндекс Бизнес: сессия протухла, требуется ручной логин",
                )

            # 4) verify-before-retry: карточка уже есть → не дублируем
            if await self._find_card(page, content.text):
                self._store.mark_done(
                    publication_id, self.channel, external_id="submitted:recovered"
                )
                return ChannelResult(
                    self.channel, ResultStatus.SUBMITTED, external_id="submitted:recovered"
                )

            # 5) вставить текст в поле публикации
            text_field = page.get_by_placeholder(_TEXT_PLACEHOLDER)
            await text_field.click()
            await text_field.fill(content.text)

            # 6) прикрепить фото (до 4), если есть
            if content.media_paths:
                await self._attach_photos(page, content.media_paths[:4])

            # 7) нажать «Создать» (ровно кнопку публикации поста)
            await self._click_create(page)

            # 8) VERIFY: ждём появления карточки с нашим текстом
            external_id = await self._wait_for_card(page, content.text)

        self._store.mark_done(publication_id, self.channel, external_id=external_id)
        # SUBMITTED: пост ушёл на модерацию. Медиа можно чистить сразу.
        return ChannelResult(self.channel, ResultStatus.SUBMITTED, external_id=external_id)

    # ── внутренние методы ────────────────────────────────────────────────────

    async def _click_create(self, page) -> None:
        """Нажать ровно кнопку публикации поста, не «Создать событие/историю».

        Сперва семантический класс button.PostAddForm-Submit (одна кнопка формы).
        Если её нет — точное имя get_by_role(..., exact=True): «Создать» без хвоста.
        """
        submit = page.locator(_SUBMIT_BUTTON_SELECTOR)
        if await submit.count() > 0:
            await submit.first.click()
            return
        await page.get_by_role("button", name=_CREATE_BUTTON_NAME, exact=True).click()

    async def _find_card(self, page, text: str) -> bool:
        """Найти карточку с данным текстом в списке публикаций (verify-before-retry)."""
        if not text.strip():
            return False
        cards = await page.locator(_CARD_TEXT_SELECTOR).all()
        for card in cards[:10]:
            try:
                card_text = await card.inner_text()
                if text.strip() in card_text:
                    return True
            except Exception:
                continue
        return False

    async def _attach_photos(self, page, paths: list[Path]) -> None:
        """Прикрепить фото через скрытый input[type=file] или expect_file_chooser.

        НЕ кликаем по видимой кнопке «Добавить» напрямую — ищем скрытый file input.
        Все пути — абсолютные.
        """
        abs_paths = [str(p.resolve()) for p in paths]
        file_input = page.locator('input[type="file"]').first
        if await file_input.count() > 0:
            await file_input.set_input_files(abs_paths)
            await self._wait_for_photos(page, len(abs_paths))
            return

        # Запасной путь: expect_file_chooser при клике на «Добавить»
        async with page.expect_file_chooser() as fc_info:
            await page.get_by_role("button", name=_PHOTO_BUTTON_NAME).click()
        fc = await fc_info.value
        await fc.set_files(abs_paths)
        await self._wait_for_photos(page, len(abs_paths))

    async def _wait_for_photos(self, page, count: int) -> None:
        """Дождаться, пока Яндекс отрисует превью всех загруженных фото.

        Превью — div.PostPhotosCollection-Photo (background-image), не <img>.
        Ждём появления хотя бы одного, затем что их число == count.
        """
        await page.wait_for_selector(_PHOTO_PREVIEW_SELECTOR, timeout=_FILE_UPLOAD_TIMEOUT)
        await page.wait_for_function(
            "([sel, n]) => document.querySelectorAll(sel).length >= n",
            arg=[_PHOTO_PREVIEW_SELECTOR, count],
            timeout=_FILE_UPLOAD_TIMEOUT,
        )

    async def _wait_for_card(self, page, text: str) -> str:
        """Дождаться появления карточки публикации, вернуть external_id."""
        await page.wait_for_function(
            """(text) => {
                const cards = document.querySelectorAll(
                    'article, [class*="post"], [class*="card"], [class*="item"]'
                );
                for (const c of cards) {
                    if (c.innerText && c.innerText.includes(text)) return true;
                }
                return false;
            }""",
            arg=text,
            timeout=_CARD_APPEAR_TIMEOUT,
        )
        # Пробуем извлечь числовой id из ссылки на карточку
        try:
            card = page.locator(_CARD_TEXT_SELECTOR).filter(has_text=text).first
            link = card.locator("a[href]").first
            href = await link.get_attribute("href") or ""
            if href:
                part = href.rstrip("/").split("/")[-1]
                if part and part.isdigit():
                    return f"yandex_post:{part}"
        except Exception:
            pass
        return "submitted"


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = YandexBrowserAdapter  # noqa: E305
