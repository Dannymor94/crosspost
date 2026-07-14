"""Тесты VKChannelBrowserAdapter («Пост в канал» — мессенджер-composer).

Это ОТДЕЛЬНЫЙ поток от стены (vk_wall): не модалка постинга, а composer
внутри раздела мессенджера vk.com/im/channels/{id}. Сессия ПЕРЕИСПОЛЬЗУЕТСЯ
из vk_wall (тот же аккаунт ВК) — open_page(..., session_channel="vk_wall").

Покрываем:
  - идемпотентный пропуск (is_done → SKIPPED)
  - NEEDS_RELOGIN при протухшей сессии
  - публикация текста: goto по URL канала, ввод в contenteditable, отправка
  - публикация с фото: set_input_files на скрытый input[type=file] в composer
  - переиспользование сессии vk_wall (session_channel)
  - build_adapter('vk_channel')
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crosspost.adapters.base import ResultStatus
from crosspost.adapters.browser.vk_channel import VKChannelBrowserAdapter
from crosspost.content.canonical import CanonicalContent, ContentType

# ── вспомогательные фабрики ─────────────────────────────────────────────────


def _make_page(*, file_input_exists: bool = True) -> AsyncMock:
    page = AsyncMock()
    page.url = "https://vk.com/im/channels/-240033402?entrypoint=channel"

    def _locator(selector, **_kw):
        loc = AsyncMock()
        loc.first = AsyncMock()
        loc.first.click = AsyncMock()
        loc.first.fill = AsyncMock()
        loc.first.type = AsyncMock()
        loc.first.press = AsyncMock()
        loc.first.inner_text = AsyncMock(return_value="")

        if "file" in selector:
            loc.first.count = AsyncMock(return_value=1 if file_input_exists else 0)
            loc.first.set_input_files = AsyncMock()
            loc.count = AsyncMock(return_value=1 if file_input_exists else 0)
        else:
            loc.count = AsyncMock(return_value=1)
            loc.first.count = AsyncMock(return_value=1)

        loc.click = AsyncMock()
        loc.fill = AsyncMock()
        loc.type = AsyncMock()
        loc.press = AsyncMock()
        loc.all = AsyncMock(return_value=[])
        return loc

    page.locator = MagicMock(side_effect=_locator)

    btn = AsyncMock()
    btn.click = AsyncMock()
    btn.count = AsyncMock(return_value=1)
    page.get_by_role = MagicMock(return_value=btn)

    text_item = AsyncMock()
    text_item.first = AsyncMock()
    text_item.first.click = AsyncMock()
    text_item.count = AsyncMock(return_value=1)
    page.get_by_text = MagicMock(return_value=text_item)

    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.goto = AsyncMock()
    page.keyboard = AsyncMock()
    page.keyboard.type = AsyncMock()
    page.keyboard.press = AsyncMock()

    return page


def _make_adapter(store) -> VKChannelBrowserAdapter:
    return VKChannelBrowserAdapter(
        channel_id="-240033402",
        store=store,
        headless=True,
    )


def _patch_open_page(page, calls: list | None = None):
    @asynccontextmanager
    async def _fake(*args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        yield page

    return patch("crosspost.adapters.browser.vk_channel.open_page", _fake)


def _patch_logged_in(value: bool):
    return patch(
        "crosspost.adapters.browser.vk_channel.is_logged_in",
        new=AsyncMock(return_value=value),
    )


# ── тесты ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_when_already_published(store, publication_id, sample_post):
    adapter = _make_adapter(store)
    store.mark_done(publication_id, "vk_channel", external_id="ch:1")

    with patch("crosspost.adapters.browser.vk_channel.open_page") as mock_op:
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    mock_op.assert_not_called()


@pytest.mark.asyncio
async def test_needs_relogin_when_not_logged_in(store, publication_id, sample_post):
    adapter = _make_adapter(store)
    page = _make_page()

    with _patch_open_page(page), _patch_logged_in(False):
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.NEEDS_RELOGIN
    assert not store.is_done(publication_id, "vk_channel")


@pytest.mark.asyncio
async def test_publishes_text_only(store, publication_id):
    adapter = _make_adapter(store)
    content = CanonicalContent(type=ContentType.POST, text="канальный пост", media_paths=[])
    page = _make_page()

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert store.is_done(publication_id, "vk_channel")

    # навигация по URL канала мессенджера
    goto_calls = [c for c in page.goto.call_args_list if "im/channels" in str(c)]
    assert goto_calls, "ожидали goto на vk.com/im/channels/{id}"

    # ввод в contenteditable composer
    composer_calls = [
        c
        for c in page.locator.call_args_list
        if "ComposerInput" in str(c) or "contenteditable" in str(c)
    ]
    assert composer_calls, "ожидали обращение к composer contenteditable"


@pytest.mark.asyncio
async def test_publishes_with_photo_calls_set_input_files(store, publication_id, sample_post):
    adapter = _make_adapter(store)
    page = _make_page(file_input_exists=True)

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    file_calls = [c for c in page.locator.call_args_list if "file" in str(c)]
    assert file_calls, "ожидали page.locator с input[type=file]"


@pytest.mark.asyncio
async def test_reuses_vk_wall_session(store, publication_id):
    adapter = _make_adapter(store)
    content = CanonicalContent(type=ContentType.POST, text="сессия", media_paths=[])
    page = _make_page()
    calls: list = []

    with _patch_open_page(page, calls), _patch_logged_in(True):
        await adapter.publish(content, publication_id=publication_id)

    # open_page вызван с session_channel="vk_wall" (переиспользование сессии)
    assert calls, "open_page не вызван"
    _, kwargs = calls[0]
    assert kwargs.get("session_channel") == "vk_wall", (
        f"ожидали session_channel='vk_wall', получили {kwargs}"
    )


@pytest.mark.asyncio
async def test_clicks_send_button_not_enter(store, publication_id):
    """Отправка через button[aria-label='Отправить сообщение'], не Enter."""
    adapter = _make_adapter(store)
    content = CanonicalContent(type=ContentType.POST, text="жми кнопку", media_paths=[])
    page = _make_page()

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    send_calls = [c for c in page.locator.call_args_list if "Отправить сообщение" in str(c)]
    assert send_calls, "ожидали locator('button[aria-label=\"Отправить сообщение\"]')"


@pytest.mark.asyncio
async def test_no_false_done_when_post_not_in_history(store, publication_id):
    """Если пост НЕ появился в PostsHistory — НЕ done, mark_done не вызывается."""
    adapter = _make_adapter(store)
    content = CanonicalContent(type=ContentType.POST, text="не ушёл", media_paths=[])
    page = _make_page()

    # PostsHistory-ожидание падает → verify не прошёл
    async def _wait_for_selector(selector, *a, **kw):
        if "PostsHistory" in selector or "Post" in selector:
            raise TimeoutError("PostsHistory пуст")
        return AsyncMock()

    page.wait_for_selector = AsyncMock(side_effect=_wait_for_selector)

    with _patch_open_page(page), _patch_logged_in(True):
        with pytest.raises(RuntimeError):
            await adapter.publish(content, publication_id=publication_id)

    assert not store.is_done(publication_id, "vk_channel")


@pytest.mark.asyncio
async def test_build_adapter_creates_vk_channel(store):
    from crosspost import __main__ as cli

    cfg = {"VK_CHANNEL_ID": "-240033402", "BROWSER_HEADLESS": "true"}
    with patch.object(cli, "load_config", return_value=cfg):
        adapter = await cli.build_adapter("vk_channel", store)

    assert isinstance(adapter, VKChannelBrowserAdapter)
    assert adapter._channel_id == "-240033402"
    assert adapter.channel == "vk_channel"
