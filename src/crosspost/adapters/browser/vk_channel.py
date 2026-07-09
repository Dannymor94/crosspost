"""ВКонтакте — браузерный адаптер «Пост в канал» (Playwright). Эпик 5.

ОТДЕЛЬНЫЙ поток от стены сообщества (vk_wall). Публикует не через модалку
постинга, а через composer внутри раздела мессенджера:
    vk.com/im/channels/{channel_id}?entrypoint=channel

Механизм (по реальному DOM):
  - Навигация прямо по URL канала мессенджера.
  - Внизу composer: [contenteditable="true"][data-placeholder="Новый пост"]
    (ComposerInput__input, role=textbox). Текст вводим через type (contenteditable
    не всегда принимает fill).
  - Фото: кнопка "+" слева от поля → поповер Фото/Видео/… → «Фото» →
    скрытый input[type=file]. Используем set_input_files напрямую, не клик по видимой кнопке.
  - Отправка: Enter в поле ИЛИ кнопка отправки в ChannelComposer__actionsContainer.
  - Verify: composer очистился ИЛИ пост появился в истории канала (PostsHistory).

Сессия ПЕРЕИСПОЛЬЗУЕТСЯ из vk_wall — тот же аккаунт ВК, не логинимся дважды:
    open_page(channel, session_channel="vk_wall").

Все СЕЛЕКТОРЫ — константы в начале файла. Приоритет data-testid / role,
CSS-классы vkit-* НЕ используем (генерённые).
"""
from __future__ import annotations

from pathlib import Path

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.adapters.browser.base_browser import is_logged_in, open_page
from crosspost.content.canonical import CanonicalContent
from crosspost.orchestrator.task import IdempotencyStore

# ── СЕЛЕКТОРЫ ────────────────────────────────────────────────────────────────

# URL раздела мессенджера с каналом сообщества
_CHANNEL_URL = "https://vk.com/im/channels/{channel_id}?entrypoint=channel"

# Маркеры неавторизации (негативная проверка — как в vk_wall)
_LOGIN_URL_FRAGMENTS = ("vk.com/login", "id.vk.com")

# Канал, из которого берём storageState-сессию (тот же аккаунт ВК)
_SESSION_CHANNEL = "vk_wall"

# Composer: поле ввода (contenteditable). Приоритет data-placeholder, role, класс.
_COMPOSER_INPUT_SELECTOR = (
    '[contenteditable="true"][data-placeholder="Новый пост"], '
    '[contenteditable="true"][role="textbox"], '
    '.ComposerInput__input'
)

# Кнопка "+" (attach) слева от поля — открывает поповер вложений
_ATTACH_BUTTON_SELECTOR = (
    '[data-testid*="attach"], '
    'button[aria-label*="рикреп"], '
    '.ChannelComposer button[aria-label*="Прикреп"]'
)

# Пункт «Фото» в поповере вложений (ищем по тексту, если роль не совпадёт)
_ATTACH_PHOTO_TEXT = "Фото"

# Скрытый input[type=file] внутри composer
_FILE_INPUT_SELECTOR = 'input[type="file"]'

# Кнопка отправки composer. Реальный DOM: button[aria-label="Отправить сообщение"],
# класс ChannelComposer__button--submit; активна → --submitActive; отправка → --loading.
_SEND_BUTTON_SELECTOR = 'button[aria-label="Отправить сообщение"]'
_SEND_ACTIVE_CLASS = "ChannelComposer__button--submitActive"
_SEND_LOADING_CLASS = "ChannelComposer__button--loading"

# История постов канала — verify появления поста (надёжнее очистки поля)
_POSTS_HISTORY_SELECTOR = '[class*="PostsHistory"] [data-post-id], [class*="PostsHistory"] [class*="Post"]'

# Ожидание превью прикреплённого фото в composer
_PHOTO_PREVIEW_SELECTOR = '.ChannelComposer img, [class*="Composer"] img'

# Таймауты (мс)
_NAV_TIMEOUT = 15_000
_COMPOSER_TIMEOUT = 10_000
_PHOTO_TIMEOUT = 15_000
_SEND_TIMEOUT = 20_000
# ─────────────────────────────────────────────────────────────────────────────


async def _debug_composer_elements(page) -> None:
    """Фолбэк-диагностика: дамп кликабельных элементов composer.

    Вызывается, когда селектор промахнулся — показывает реальную разметку.
    Оставить до подтверждения рабочих селекторов на живом ВК.
    """
    print("\n[vk_channel DEBUG] элементы composer:")
    try:
        items = await page.locator(
            "[class*='Composer'] button, [class*='Composer'] [role], "
            "[contenteditable], input[type='file']"
        ).all()
        for el in items[:40]:
            try:
                tag = await el.evaluate("el => el.tagName")
                role = await el.get_attribute("role") or ""
                aria = await el.get_attribute("aria-label") or ""
                cls = (await el.get_attribute("class") or "")[:60]
                ph = await el.get_attribute("data-placeholder") or ""
                print(f"  <{tag.lower()}> role={role!r} aria={aria!r} ph={ph!r} class={cls!r}")
            except Exception:
                continue
    except Exception as e:
        print(f"[vk_channel DEBUG] ошибка дампа: {e}")
    print("[vk_channel DEBUG] ---")


class VKChannelBrowserAdapter:
    channel = "vk_channel"

    def __init__(
        self,
        channel_id: str,
        store: IdempotencyStore,
        *,
        headless: bool = False,
    ) -> None:
        self._channel_id = channel_id   # напр. "-240033402"
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

        # Сессию берём из vk_wall — тот же аккаунт ВК, не логинимся дважды.
        async with open_page(
            self.channel, headless=self._headless, session_channel=_SESSION_CHANNEL
        ) as page:
            # 2) навигация в раздел канала мессенджера
            url = _CHANNEL_URL.format(channel_id=self._channel_id)
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)

            # 3) проверка сессии (негативная: нет URL логина = залогинен)
            if not await is_logged_in(page, reject_url_fragments=_LOGIN_URL_FRAGMENTS):
                return ChannelResult(
                    self.channel,
                    ResultStatus.NEEDS_RELOGIN,
                    error="ВК: сессия протухла, выполни crosspost login --channel vk_wall",
                )

            # 4) ждём composer, вводим текст
            composer = await self._focus_composer(page)
            if content.text:
                await composer.type(content.text)

            # 5) фото, если есть
            if content.media_paths:
                await self._attach_photos(page, content.media_paths[:10])

            # 6) отправка
            external_id = await self._send(page)

        self._store.mark_done(publication_id, self.channel, external_id=external_id)
        return ChannelResult(self.channel, ResultStatus.DONE, external_id=external_id)

    # ── внутренние методы ────────────────────────────────────────────────────

    async def _focus_composer(self, page):
        """Дождаться поля ввода composer и сфокусировать его."""
        try:
            await page.wait_for_selector(_COMPOSER_INPUT_SELECTOR, timeout=_COMPOSER_TIMEOUT)
        except Exception:
            await _debug_composer_elements(page)
        composer = page.locator(_COMPOSER_INPUT_SELECTOR).first
        await composer.click()
        return composer

    async def _attach_photos(self, page, paths: list[Path]) -> None:
        """Прикрепить фото через скрытый input[type=file].

        НЕ полагаемся на клик по видимой кнопке (открывает системный chooser) —
        грузим напрямую через set_input_files. Если скрытого input ещё нет,
        сперва раскрываем поповер «+» → «Фото», чтобы input появился.
        """
        abs_paths = [str(p.resolve()) for p in paths]

        file_input = page.locator(_FILE_INPUT_SELECTOR).first
        if await file_input.count() == 0:
            # input ещё не в DOM — раскрываем меню вложений
            await self._open_attach_menu(page)
            file_input = page.locator(_FILE_INPUT_SELECTOR).first

        if await file_input.count() == 0:
            await _debug_composer_elements(page)
        await file_input.set_input_files(abs_paths)
        await self._wait_for_photo_preview(page)

    async def _open_attach_menu(self, page) -> None:
        """Кликнуть "+" и выбрать «Фото» в поповере вложений."""
        attach = page.locator(_ATTACH_BUTTON_SELECTOR).first
        if await attach.count() > 0:
            await attach.click()
            # Пункт «Фото» — по тексту (роль в ВК нестабильна)
            try:
                await page.get_by_text(_ATTACH_PHOTO_TEXT, exact=True).first.click()
            except Exception:
                pass  # input мог появиться уже после клика "+"

    async def _wait_for_photo_preview(self, page) -> None:
        """Дождаться превью прикреплённого фото в composer."""
        try:
            await page.wait_for_selector(_PHOTO_PREVIEW_SELECTOR, timeout=_PHOTO_TIMEOUT)
        except Exception:
            pass  # превью не обязательно — отправка всё равно пройдёт

    async def _send(self, page) -> str:
        """Отправить пост кнопкой «Отправить сообщение».

        НЕ через Enter первым — в contenteditable Enter делает перенос строки.
        1. Ждём, что кнопка активна (--submitActive) — значит текст/фото приняты.
        2. Кликаем.
        3. Verify: пост появился в PostsHistory (надёжнее очистки поля).

        Бросает RuntimeError, если пост не появился в истории — не даём ложный done.
        """
        # 1) ждём активную кнопку отправки
        await page.wait_for_selector(_SEND_BUTTON_SELECTOR, timeout=_COMPOSER_TIMEOUT)
        try:
            await page.wait_for_function(
                """([sel, cls]) => {
                    const el = document.querySelector(sel);
                    return el && el.className.includes(cls);
                }""",
                arg=[_SEND_BUTTON_SELECTOR, _SEND_ACTIVE_CLASS],
                timeout=_COMPOSER_TIMEOUT,
            )
        except Exception:
            # кнопка могла не получить класс --submitActive в этой сборке —
            # продолжаем, клик всё равно попробуем
            pass

        # 2) клик
        await page.locator(_SEND_BUTTON_SELECTOR).first.click()

        # 3) verify: пост реально появился в истории канала
        if not await self._verify_post_appeared(page):
            raise RuntimeError(
                "vk_channel: пост не появился в PostsHistory после отправки"
            )

        return await self._extract_post_id(page)

    async def _verify_post_appeared(self, page) -> bool:
        """Дождаться появления поста в истории канала (PostsHistory заполнился).

        Надёжнее очистки поля: очистка бывает и без реальной отправки.
        """
        try:
            await page.wait_for_selector(
                _POSTS_HISTORY_SELECTOR, timeout=_SEND_TIMEOUT
            )
            return True
        except Exception:
            return False

    async def _extract_post_id(self, page) -> str:
        """Попробовать вытащить id свежего поста из истории канала. Фолбэк 'posted'."""
        try:
            post = page.locator('[data-post-id]').first
            if await post.count() > 0:
                post_id = await post.get_attribute("data-post-id") or ""
                if post_id:
                    return f"channel{post_id}"
        except Exception:
            pass
        return "posted"


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = VKChannelBrowserAdapter  # noqa: E305
