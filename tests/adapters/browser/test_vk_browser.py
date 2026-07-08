"""Тесты VKBrowserAdapter — без реального браузера.

Playwright подменяется моком: open_page() пропатчен в модуле адаптера
через monkeypatch. page — AsyncMock с нужными методами.

Контракт: те же инварианты, что у API-адаптеров:
  - идемпотентный пропуск (is_done → SKIPPED, браузер не открывается)
  - verify-before-retry: пост уже в DOM → DONE без отправки
  - нормальная публикация: текст + фото → external_id из href или "posted"
"""
from __future__ import annotations

from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crosspost.adapters.browser.vk import VKBrowserAdapter
from crosspost.adapters.base import ResultStatus


# ── фикстуры ────────────────────────────────────────────────────────────────

@pytest.fixture
def adapter(store, tmp_path) -> VKBrowserAdapter:
    return VKBrowserAdapter(
        group_id=12345,
        store=store,
        headless=True,
    )


def _make_page(
    *,
    post_exists: bool = False,
    post_href: str = "/wall-12345_42",
) -> MagicMock:
    """Сконструировать мок страницы Playwright."""
    page = AsyncMock()

    # locator() → объект с .first, .all(), .count(), .fill(), .set_input_files()
    def _locator(selector, **_kw):
        loc = AsyncMock()
        loc.first = AsyncMock()
        loc.first.count = AsyncMock(return_value=1)
        loc.first.inner_text = AsyncMock(return_value="")
        loc.first.click = AsyncMock()
        loc.first.fill = AsyncMock()
        loc.first.set_input_files = AsyncMock()
        loc.first.get_attribute = AsyncMock(return_value=post_href)
        loc.count = AsyncMock(return_value=1)

        # .all() для _post_already_exists — список пост-элементов
        wall_text_mock = AsyncMock()
        wall_text_mock.inner_text = AsyncMock(
            return_value="тестовый пост" if post_exists else "другой пост"
        )
        loc.all = AsyncMock(return_value=[wall_text_mock])
        return loc

    page.locator = _locator

    # get_by_role() — кнопки
    btn = AsyncMock()
    btn.count = AsyncMock(return_value=1)
    btn.click = AsyncMock()
    page.get_by_role = MagicMock(return_value=btn)

    # wait_for_selector / wait_for_function
    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()

    return page


@pytest.fixture
def mock_open_page(adapter):
    """Патчим open_page в модуле vk-браузерного адаптера."""
    page = _make_page()

    @asynccontextmanager
    async def _fake_open_page(*args, **kwargs):
        yield page

    with patch("crosspost.adapters.browser.vk.open_page", _fake_open_page):
        yield page


@pytest.fixture
def mock_open_page_with_existing_post():
    """open_page с постом уже в DOM (verify-before-retry кейс)."""
    page = _make_page(post_exists=True)

    @asynccontextmanager
    async def _fake(*args, **kwargs):
        yield page

    with patch("crosspost.adapters.browser.vk.open_page", _fake):
        yield page


# ── тесты ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skips_when_already_published(store, publication_id, sample_post, adapter):
    """is_done → SKIPPED, браузер не открывается."""
    store.mark_done(publication_id, "vk", external_id="wall-12345_10")

    with patch("crosspost.adapters.browser.vk.open_page") as mock_op:
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    mock_op.assert_not_called()


@pytest.mark.asyncio
async def test_verify_before_retry_finds_existing_post(
    store, publication_id, sample_post, adapter, mock_open_page_with_existing_post
):
    """Verify-before-retry: пост уже в DOM — не дублируем, помечаем done."""
    # sample_post.text = "Привет" (из conftest), мок вернёт "тестовый пост" — не совпадёт.
    # Нужно пост с текстом, который есть в DOM.
    from crosspost.content.canonical import CanonicalContent, ContentType
    content_with_match = CanonicalContent(
        type=ContentType.POST,
        text="тестовый пост",
        media_paths=[],
    )

    result = await adapter.publish(content_with_match, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "posted:recovered"
    assert store.is_done(publication_id, "vk")
    # wait_for_function НЕ вызывался — не дошли до submit
    mock_open_page_with_existing_post.wait_for_function.assert_not_called()


@pytest.mark.asyncio
async def test_publishes_and_returns_wall_id(
    store, publication_id, sample_post, adapter, mock_open_page
):
    """Нормальная публикация: external_id берётся из href (wall-{group}_{id})."""
    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "12345_42"  # из href "/wall-12345_42"
    assert store.is_done(publication_id, "vk")
    # страница была открыта и пост отправлен
    mock_open_page.goto.assert_called_once()
    mock_open_page.wait_for_function.assert_called_once()


@pytest.mark.asyncio
async def test_publishes_without_photo(store, publication_id, adapter):
    """Пост без медиа — attach_photo не вызывается."""
    from crosspost.content.canonical import CanonicalContent, ContentType
    text_only = CanonicalContent(type=ContentType.POST, text="только текст", media_paths=[])
    page = _make_page()

    @asynccontextmanager
    async def _fake(*args, **kwargs):
        yield page

    with patch("crosspost.adapters.browser.vk.open_page", _fake):
        result = await adapter.publish(text_only, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    # set_input_files не вызывался (нет медиа)
    assert not any(
        call for call in page.mock_calls if "set_input_files" in str(call)
    )


@pytest.mark.asyncio
async def test_fallback_external_id_when_no_href(store, publication_id, sample_post, adapter):
    """Если href не содержит 'wall-' — external_id = 'posted'."""
    page = _make_page(post_href="/id12345")  # href без wall-

    @asynccontextmanager
    async def _fake(*args, **kwargs):
        yield page

    with patch("crosspost.adapters.browser.vk.open_page", _fake):
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "posted"
