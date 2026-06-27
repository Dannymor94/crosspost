"""Тесты ВК-адаптера (фаза 0, шаг 3 — переписан под РЕАЛЬНЫЙ vkbottle).

Реальная форма vkbottle (сверено с исходниками vkbottle):
  uploader = PhotoWallUploader(api)                  # конструктор берёт api
  attachment = await uploader.upload(path, group_id=...)  # -> "photo{owner}_{id}"
  posted = await api.wall.post(owner_id=, message=, attachments=)  # snake_case
  external_id = posted.post_id                       # квитанция

Мок повторяет именно эти вызовы (не выдуманный camelCase). vkbottle подменяется
через sys.modules — ставить пакет не нужно.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from crosspost.adapters.api.vk import VKAdapter
from crosspost.adapters.base import ResultStatus


@pytest.fixture
def fake_vk_api() -> AsyncMock:
    """Мок vkbottle.API: нужен только wall.post, возвращающий объект с post_id."""
    api = AsyncMock()
    posted = AsyncMock()
    posted.post_id = 777  # post_id — это и есть квитанция
    api.wall.post.return_value = posted
    return api


@pytest.fixture
def fake_vkbottle(monkeypatch) -> types.ModuleType:
    """Подменяем vkbottle: PhotoWallUploader(api).upload(...) -> attachment-строка."""
    mod = types.ModuleType("vkbottle")
    uploader = MagicMock(name="PhotoWallUploader()")
    # upload — корутина, реальный возврат — готовая attachment-строка
    uploader.upload = AsyncMock(return_value="photo-100_555")
    mod.PhotoWallUploader = MagicMock(name="PhotoWallUploader", return_value=uploader)
    monkeypatch.setitem(sys.modules, "vkbottle", mod)
    return mod


@pytest.mark.asyncio
async def test_skips_when_already_published(
    store, publication_id, sample_post, fake_vk_api, fake_vkbottle
):
    """Если (publication_id, channel) уже done — не публикуем (даже uploader не строим)."""
    store.mark_done(publication_id, "vk", external_id="999")
    adapter = VKAdapter(fake_vk_api, target="-100", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    fake_vkbottle.PhotoWallUploader.assert_not_called()
    fake_vk_api.wall.post.assert_not_called()


@pytest.mark.asyncio
async def test_publishes_and_returns_receipt(
    store, publication_id, sample_post, fake_vk_api, fake_vkbottle
):
    """RED: публикация грузит фото через PhotoWallUploader и постит wall.post.

    Квитанция — post_id из wall.post, а attachment из загрузки уходит в пост.
    """
    adapter = VKAdapter(fake_vk_api, target="-100", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "777"  # post_id, не id фото

    # PhotoWallUploader(api) — конструктор получает наш api
    fake_vkbottle.PhotoWallUploader.assert_called_once_with(fake_vk_api)

    # upload(path, group_id=abs(owner_id)) — реальная сигнатура
    uploader = fake_vkbottle.PhotoWallUploader.return_value
    uploader.upload.assert_awaited_once()
    assert uploader.upload.call_args.kwargs["group_id"] == 100

    # attachment из загрузки уходит в wall.post, owner_id — стена сообщества
    fake_vk_api.wall.post.assert_awaited_once()
    post_kwargs = fake_vk_api.wall.post.call_args.kwargs
    assert post_kwargs["owner_id"] == -100
    assert post_kwargs["attachments"] == "photo-100_555"

    assert store.is_done(publication_id, "vk")  # помечено done
