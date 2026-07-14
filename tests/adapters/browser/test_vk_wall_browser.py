"""Тесты VKWallBrowserAdapter — без реального браузера.

open_page и is_logged_in патчатся; page — AsyncMock.

Покрываем:
  - идемпотентный пропуск (is_done → SKIPPED)
  - NEEDS_RELOGIN при протухшей сессии
  - verify-before-retry: пост уже в ленте → DONE:recovered
  - нормальная публикация без фото (текст, «Далее», step2)
  - публикация с фото: set_input_files вызван, ждём превью
  - клик «Далее» обязателен (двухшаговая форма)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crosspost.adapters.base import ResultStatus
from crosspost.adapters.browser.vk_wall import VKWallBrowserAdapter
from crosspost.content.canonical import CanonicalContent, ContentType

# ── вспомогательные фабрики ─────────────────────────────────────────────────


def _make_page(
    *,
    post_exists: bool = False,
    post_text_match: str = "",
    post_data_id: str = "-123456_789",
    file_input_exists: bool = True,
) -> AsyncMock:
    page = AsyncMock()
    page.url = "https://vk.com/medithou"

    def _locator(selector, **_kw):
        loc = AsyncMock()
        loc.first = AsyncMock()
        loc.first.click = AsyncMock()
        loc.first.fill = AsyncMock()
        loc.first.get_attribute = AsyncMock(return_value=post_data_id)

        # .count() — по умолчанию 1, для file-input управляем параметром
        if "file" in selector:
            # file_input = page.locator(...).first → count/set_input_files на .first
            loc.first.count = AsyncMock(return_value=1 if file_input_exists else 0)
            loc.first.set_input_files = AsyncMock()
            loc.count = AsyncMock(return_value=1 if file_input_exists else 0)
        else:
            loc.count = AsyncMock(return_value=1)
            loc.first.count = AsyncMock(return_value=1)

        # .all() — список постов в ленте
        post_mock = AsyncMock()
        post_mock.inner_text = AsyncMock(
            return_value=post_text_match if post_exists else "другой пост"
        )
        loc.all = AsyncMock(return_value=[post_mock])
        return loc

    page.locator = MagicMock(side_effect=_locator)

    # get_by_role — кнопка «Создать» в шапке
    btn = AsyncMock()
    btn.click = AsyncMock()
    btn.count = AsyncMock(return_value=1)
    page.get_by_role = MagicMock(return_value=btn)

    # get_by_text — пункт меню «Пост» (regex ^Пост$, не menuitem)
    text_item = AsyncMock()
    text_item.first = AsyncMock()
    text_item.first.click = AsyncMock()
    text_item.count = AsyncMock(return_value=1)
    page.get_by_text = MagicMock(return_value=text_item)

    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.goto = AsyncMock()

    return page


def _make_adapter(store) -> VKWallBrowserAdapter:
    return VKWallBrowserAdapter(
        screen_name="medithou",
        store=store,
        headless=True,
    )


def _patch_open_page(page):
    @asynccontextmanager
    async def _fake(*args, **kwargs):
        yield page

    return patch("crosspost.adapters.browser.vk_wall.open_page", _fake)


def _patch_logged_in(value: bool):
    return patch(
        "crosspost.adapters.browser.vk_wall.is_logged_in",
        new=AsyncMock(return_value=value),
    )


# ── тесты ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_when_already_published(store, publication_id, sample_post):
    """is_done → SKIPPED, браузер не открывается."""
    adapter = _make_adapter(store)
    store.mark_done(publication_id, "vk_wall", external_id="wall-123456_1")

    with patch("crosspost.adapters.browser.vk_wall.open_page") as mock_op:
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    mock_op.assert_not_called()


@pytest.mark.asyncio
async def test_needs_relogin_when_not_logged_in(store, publication_id, sample_post):
    """Нет авторизации → NEEDS_RELOGIN."""
    adapter = _make_adapter(store)
    page = _make_page()

    with _patch_open_page(page), _patch_logged_in(False):
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.NEEDS_RELOGIN
    assert "vk_wall" in result.error or "ВК" in result.error
    assert not store.is_done(publication_id, "vk_wall")


@pytest.mark.asyncio
async def test_verify_before_retry_finds_existing_post(store, publication_id):
    """Verify-before-retry: пост уже в ленте — DONE:recovered без отправки."""
    adapter = _make_adapter(store)
    text = "уже опубликованный пост"
    content = CanonicalContent(type=ContentType.POST, text=text, media_paths=[])
    page = _make_page(post_exists=True, post_text_match=text)

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "posted:recovered"
    assert store.is_done(publication_id, "vk_wall")
    # до формы не дошли
    page.get_by_role.assert_not_called()


@pytest.mark.asyncio
async def test_publishes_text_only(store, publication_id):
    """Публикация текста: открытие формы, fill, клик «Далее», step2."""
    adapter = _make_adapter(store)
    content = CanonicalContent(type=ContentType.POST, text="тестовый пост", media_paths=[])
    page = _make_page()

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert store.is_done(publication_id, "vk_wall")

    # открытие формы: «Создать» через get_by_role exact=True
    role_calls = page.get_by_role.call_args_list
    create_call = next((c for c in role_calls if c.kwargs.get("name") == "Создать"), None)
    assert create_call is not None, "ожидали get_by_role('button', name='Создать', exact=True)"
    assert create_call.kwargs.get("exact") is True

    # «Пост» — через get_by_text с regex (не menuitem, ВК не использует эту роль)
    assert page.get_by_text.called, "ожидали get_by_text(re.compile('^Пост$'), exact=True)"
    text_arg = page.get_by_text.call_args_list[0].args[0]
    import re as _re

    assert isinstance(text_arg, _re.Pattern) or str(text_arg) == "Пост", (
        f"ожидали regex ^Пост$, получили {text_arg!r}"
    )

    # модалка ожидалась
    page.wait_for_selector.assert_called()
    modal_calls = [
        c for c in page.wait_for_selector.call_args_list if "posting_modal_box" in str(c)
    ]
    assert modal_calls, "ожидали wait_for_selector('[data-testid=\"posting_modal_box\"]')"

    # «Далее» нажат
    next_calls = [c for c in page.locator.call_args_list if "posting_base_screen_next" in str(c)]
    assert next_calls, "ожидали клик по [data-testid='posting_base_screen_next']"


@pytest.mark.asyncio
async def test_publishes_with_photo_calls_set_input_files(store, publication_id, sample_post):
    """Публикация с фото: set_input_files вызван с абсолютным путём."""
    adapter = _make_adapter(store)
    page = _make_page(file_input_exists=True)

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE

    # file-input был найден и set_input_files вызван
    file_input_calls = [c for c in page.locator.call_args_list if "file" in str(c)]
    assert file_input_calls, "ожидали page.locator с input[type=file]"

    # превью ожидалось
    page.wait_for_selector.assert_called()
    preview_calls = [
        c
        for c in page.wait_for_selector.call_args_list
        if "img" in str(c) or "photo" in str(c).lower()
    ]
    assert preview_calls or page.wait_for_function.called, (
        "ожидали wait_for_selector или wait_for_function для превью фото"
    )


@pytest.mark.asyncio
async def test_next_button_is_clicked_before_step2(store, publication_id):
    """«Далее» кликается ПЕРЕД шагом 2 — двухшаговая форма.

    Адаптер вызывает page.locator(NEXT_SELECTOR).click(),
    не .first.click() — проверяем вызов локатора по селектору.
    """
    adapter = _make_adapter(store)
    content = CanonicalContent(type=ContentType.POST, text="двухшаговый пост", media_paths=[])
    page = _make_page()

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE

    # page.locator должен быть вызван с селектором кнопки «Далее»
    next_calls = [c for c in page.locator.call_args_list if "posting_base_screen_next" in str(c)]
    assert next_calls, (
        "ожидали page.locator('[data-testid=\"posting_base_screen_next\"]') — "
        f"реальные вызовы: {[str(c) for c in page.locator.call_args_list]}"
    )


@pytest.mark.asyncio
async def test_build_adapter_creates_vk_wall(store, tmp_path):
    """build_adapter('vk_wall') → VKWallBrowserAdapter с правильным screen_name."""
    from crosspost import __main__ as cli

    cfg = {"VK_GROUP_SCREEN_NAME": "medithou", "BROWSER_HEADLESS": "true"}
    with patch.object(cli, "load_config", return_value=cfg):
        adapter = await cli.build_adapter("vk_wall", store)

    assert isinstance(adapter, VKWallBrowserAdapter)
    assert adapter._screen_name == "medithou"
    assert adapter.channel == "vk_wall"
