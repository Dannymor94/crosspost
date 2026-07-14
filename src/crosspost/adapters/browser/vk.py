"""ВК-браузерный адаптер. Эпик 5.

Публикует пост в сообщество через реальный браузер (Playwright).
Используется вместо API-адаптера, пока community/user-токен VK недоступен.

Контракт: async publish(content, *, publication_id) -> ChannelResult — тот же,
что у TelegramAdapter и VKAdapter (adapters/api).

Verify-before-retry (CLAUDE.md инвариант 2 для браузерного тира):
  1. Перед публикацией: если store.is_done → SKIPPED.
  2. После перехода на страницу, при publication_id == attempting:
     проверить DOM ленты на наличие поста с нашим текстом, прежде чем жать «Опубликовать».
     Это закрывает окно «послали → упали до mark_done».

headless берётся из конфига (BROWSER_HEADLESS). По умолчанию False — для первого
ручного логина и отладки.

Playwright импортируется ЛЕНИВО через base_browser.open_page — не тащим в шапку.
"""

from __future__ import annotations

from pathlib import Path

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.adapters.browser.base_browser import open_page
from crosspost.content.canonical import CanonicalContent
from crosspost.orchestrator.task import IdempotencyStore

# URL ленты сообщества: vk.com/public{group_id} или vk.com/club{group_id}
_GROUP_URL = "https://vk.com/public{group_id}"


class VKBrowserAdapter:
    channel = "vk"

    def __init__(
        self,
        group_id: int,
        store: IdempotencyStore,
        *,
        headless: bool = False,
    ) -> None:
        self._group_id = group_id  # положительное число (без минуса)
        self._store = store
        self._headless = headless

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult:
        # 1) идемпотентность — дедуп по внутреннему ключу
        if self._store.is_done(publication_id, self.channel):
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        async with open_page(self.channel, headless=self._headless) as page:
            group_url = _GROUP_URL.format(group_id=self._group_id)
            await page.goto(group_url, wait_until="domcontentloaded")

            # 2) verify-before-retry: если пост с нашим текстом уже есть в ленте —
            #    не дублируем (закрываем окно «послали → упали до mark_done»).
            if await self._post_already_exists(page, content.text):
                # нашли в DOM — помечаем done с квитанцией "posted" и выходим
                self._store.mark_done(publication_id, self.channel, external_id="posted:recovered")
                return ChannelResult(
                    self.channel, ResultStatus.DONE, external_id="posted:recovered"
                )

            # 3) открыть форму создания поста
            await self._open_post_form(page)

            # 4) вставить текст
            await page.locator('div[contenteditable="true"]').first.fill(content.text)

            # 5) прикрепить фото, если есть
            if content.media_paths:
                await self._attach_photo(page, content.media_paths[0])

            # 6) отправить и дождаться публикации
            external_id = await self._submit_and_get_id(page, content.text)

        self._store.mark_done(publication_id, self.channel, external_id=external_id)
        return ChannelResult(self.channel, ResultStatus.DONE, external_id=external_id)

    # ── внутренние методы ────────────────────────────────────────────────────

    async def _post_already_exists(self, page, text: str) -> bool:
        """Проверить, есть ли пост с данным текстом в верхних записях ленты."""
        # ищем первые N постов в ленте сообщества
        posts = await page.locator("div.wall_text").all()
        for post in posts[:5]:  # проверяем только верхние — дальние нас не интересуют
            try:
                post_text = await post.inner_text()
                if text.strip() and text.strip() in post_text:
                    return True
            except Exception:
                continue
        return False

    async def _open_post_form(self, page) -> None:
        """Кликнуть «Что у вас нового?» или кнопку «Написать пост» в сообществе."""
        # Пробуем разные селекторы — VK периодически переименовывает классы
        for selector in [
            "div.post_write_wrap",
            "div.write_wall_fake_input",
            '[placeholder*="нового"]',
        ]:
            loc = page.locator(selector).first
            if await loc.count() > 0:
                await loc.click()
                return
        # запасной вариант — кнопка «Предложить новость» / «Написать пост»
        await page.get_by_role("button", name="Написать").first.click()

    async def _attach_photo(self, page, photo_path: Path) -> None:
        """Прикрепить фото через скрытый file-input."""
        # кликаем иконку «Фото» в тулбаре
        for selector in [
            'input[type="file"][accept*="image"]',
            'label[data-type="photo"] input[type="file"]',
        ]:
            loc = page.locator(selector).first
            if await loc.count() > 0:
                await loc.set_input_files(str(photo_path))
                # ждём появления превью загруженного фото
                await page.wait_for_selector(
                    "div.upload_img_preview, div.wall_photo_el", timeout=15_000
                )
                return
        # если скрытый input не нашли — кликаем кнопку «Фото» и потом выбираем
        await page.get_by_role("button", name="Фото").click()
        await page.locator('input[type="file"]').first.set_input_files(str(photo_path))
        await page.wait_for_selector("div.upload_img_preview", timeout=15_000)

    async def _submit_and_get_id(self, page, text: str) -> str:
        """Нажать «Опубликовать», дождаться появления поста, вернуть external_id."""
        # кнопка «Опубликовать» / «Отправить»
        for name in ["Опубликовать", "Отправить", "Поделиться"]:
            btn = page.get_by_role("button", name=name)
            if await btn.count() > 0:
                await btn.click()
                break

        # ждём появления нового поста в ленте (максимум 20 сек)
        await page.wait_for_function(
            """(text) => {
                const posts = document.querySelectorAll('div.wall_text');
                for (const p of posts) {
                    if (p.innerText.includes(text)) return true;
                }
                return false;
            }""",
            arg=text,
            timeout=20_000,
        )

        # пробуем извлечь числовой id из ссылки на пост
        try:
            link = page.locator("a.post__anchor, a[href*='wall-']").first
            href = await link.get_attribute("href") or ""
            # href вида /wall-12345_678 → external_id = "wall-12345_678"
            if "wall-" in href:
                return href.split("wall-")[1].split("?")[0]
        except Exception:
            pass
        return "posted"


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = VKBrowserAdapter  # noqa: E305
