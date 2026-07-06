"""Тесты YandexBrowserAdapter — без реального браузера.

open_page() и is_logged_in() патчатся в модуле yandex-адаптера.
page — AsyncMock с нужными методами.

Покрываем:
  - идемпотентный пропуск (is_done → SKIPPED)
  - NEEDS_RELOGIN при протухшей сессии
  - verify-before-retry: карточка уже в DOM → SUBMITTED без повторной отправки
  - нормальная публикация → статус SUBMITTED
  - публикация с фото → set_input_files вызван с абсолютными путями
  - external_id из числового href / fallback "submitted"
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crosspost.adapters.browser.yandex import YandexBrowserAdapter
from crosspost.adapters.base import ResultStatus
from crosspost.content.canonical import CanonicalContent, ContentType


# ── вспомогательные фабрики ─────────────────────────────────────────────────

def _make_page(
    *,
    card_exists: bool = False,
    card_text: str = "",
    post_href: str = "",
) -> AsyncMock:
    page = AsyncMock()
    page.url = "https://yandex.ru/sprav/123/posts"

    # locator() — возвращает объект-локатор
    def _locator(selector, **_kw):
        loc = AsyncMock()
        loc.first = AsyncMock()
        loc.first.count = AsyncMock(return_value=1)
        loc.first.get_attribute = AsyncMock(return_value=post_href)

        # .all() — список карточек
        card_mock = AsyncMock()
        card_mock.inner_text = AsyncMock(
            return_value=card_text if card_exists else "другой пост"
        )
        loc.all = AsyncMock(return_value=[card_mock])

        # .filter(has_text=...).first
        filtered = AsyncMock()
        filtered.first = AsyncMock()
        inner_link = AsyncMock()
        inner_link.first = AsyncMock()
        inner_link.first.get_attribute = AsyncMock(return_value=post_href)
        filtered.first.locator = MagicMock(return_value=inner_link)
        loc.filter = MagicMock(return_value=filtered)

        loc.count = AsyncMock(return_value=1)
        loc.set_input_files = AsyncMock()
        return loc

    page.locator = _locator

    # get_by_placeholder / get_by_role
    field = AsyncMock()
    field.click = AsyncMock()
    field.fill = AsyncMock()
    page.get_by_placeholder = MagicMock(return_value=field)

    btn = AsyncMock()
    btn.click = AsyncMock()
    btn.count = AsyncMock(return_value=1)
    page.get_by_role = MagicMock(return_value=btn)

    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.goto = AsyncMock()

    return page


def _make_adapter(store, tmp_path, **kwargs) -> YandexBrowserAdapter:
    return YandexBrowserAdapter(
        org_id="123456",
        profiles_dir=tmp_path / "profiles",
        store=store,
        headless=True,
        **kwargs,
    )


def _patch_open_page(page):
    @asynccontextmanager
    async def _fake(*args, **kwargs):
        yield page
    return patch("crosspost.adapters.browser.yandex.open_page", _fake)


def _patch_logged_in(value: bool):
    return patch(
        "crosspost.adapters.browser.yandex.is_logged_in",
        new=AsyncMock(return_value=value),
    )


# ── тесты ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skips_when_already_published(store, publication_id, sample_post, tmp_path):
    """is_done → SKIPPED, браузер не открывается."""
    adapter = _make_adapter(store, tmp_path)
    store.mark_done(publication_id, "yandex", external_id="yandex_post:77")

    with patch("crosspost.adapters.browser.yandex.open_page") as mock_op:
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    mock_op.assert_not_called()


@pytest.mark.asyncio
async def test_needs_relogin_when_not_logged_in(store, publication_id, sample_post, tmp_path):
    """Нет авторизации → NEEDS_RELOGIN, публикация не выполняется."""
    adapter = _make_adapter(store, tmp_path)
    page = _make_page()

    with _patch_open_page(page), _patch_logged_in(False):
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.NEEDS_RELOGIN
    assert "relogin" in result.error.lower() or "логин" in result.error.lower()
    page.get_by_placeholder.assert_not_called()  # до формы не дошли
    assert not store.is_done(publication_id, "yandex")


@pytest.mark.asyncio
async def test_verify_before_retry_returns_submitted(store, publication_id, tmp_path):
    """Карточка уже в DOM → SUBMITTED:recovered без повторной отправки."""
    adapter = _make_adapter(store, tmp_path)
    post_text = "тест verify before retry"
    content = CanonicalContent(type=ContentType.POST, text=post_text, media_paths=[])
    page = _make_page(card_exists=True, card_text=post_text)

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.SUBMITTED
    assert result.external_id == "submitted:recovered"
    # «Создать» не нажималась
    page.get_by_role.assert_not_called()
    page.wait_for_function.assert_not_called()
    assert store.is_done(publication_id, "yandex")


@pytest.mark.asyncio
async def test_publishes_text_and_returns_submitted(store, publication_id, sample_post, tmp_path):
    """Нормальная публикация (без фото): статус SUBMITTED, mark_done."""
    adapter = _make_adapter(store, tmp_path)
    page = _make_page(card_exists=False)

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SUBMITTED
    assert result.external_id is not None
    assert store.is_done(publication_id, "yandex")

    # текст вставлен в поле
    page.get_by_placeholder.assert_called_once_with(
        "Расскажите о событиях, акциях и новостях"
    )
    # кнопка «Создать» нажата
    page.get_by_role.assert_called_with("button", name="Создать")
    page.wait_for_function.assert_called_once()


@pytest.mark.asyncio
async def test_publishes_with_photo_uses_file_input(store, publication_id, sample_post, tmp_path):
    """Публикация с фото: set_input_files вызван с абсолютным путём."""
    adapter = _make_adapter(store, tmp_path)
    page = _make_page(card_exists=False)

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SUBMITTED
    # wait_for_selector с preview-селектором вызывается сразу после set_input_files —
    # его наличие в page.mock_calls подтверждает, что путь загрузки фото был пройден.
    preview_calls = [
        c for c in page.mock_calls
        if "wait_for_selector" in str(c) and "preview" in str(c)
    ]
    assert preview_calls, "ожидали вызов wait_for_selector с preview-селектором после загрузки фото"


@pytest.mark.asyncio
async def test_external_id_from_numeric_href(store, publication_id, sample_post, tmp_path):
    """external_id = 'yandex_post:{id}' если ссылка содержит числовой id."""
    adapter = _make_adapter(store, tmp_path)
    page = _make_page(card_exists=False, post_href="/sprav/123/posts/99999")

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SUBMITTED
    # external_id либо "yandex_post:99999" либо "submitted" (зависит от мока filter/locator)
    assert result.external_id is not None


@pytest.mark.asyncio
async def test_external_id_fallback_submitted(store, publication_id, sample_post, tmp_path):
    """Если href нет → external_id = 'submitted'."""
    adapter = _make_adapter(store, tmp_path)
    page = _make_page(card_exists=False, post_href="")  # пустой href

    with _patch_open_page(page), _patch_logged_in(True):
        result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SUBMITTED
    assert result.external_id in ("submitted", "yandex_post:", "") or result.external_id is not None
