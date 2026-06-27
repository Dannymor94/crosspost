"""Тесты ВК-адаптера (фаза 0, шаг 3, TDD). По образцу test_telegram.py.

Публикация в ВК — МНОГОШАГОВАЯ:
  photos.getWallUploadServer -> POST файла на upload-сервер
  -> photos.saveWallPhoto -> wall.post.
Квитанция (external_id) — это post_id из wall.post, НЕ из шага загрузки фото.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from crosspost.adapters.api.vk import VKAdapter
from crosspost.adapters.base import ResultStatus


@pytest.fixture
def fake_vk_api() -> AsyncMock:
    """Мок VK API: три метода многошаговой публикации.

    Намеренно даём шагу загрузки фото свои id (server/photo id 555),
    а wall.post — свой post_id 777, чтобы тест отличал квитанцию от мусора.
    """
    api = AsyncMock()

    server = AsyncMock()
    server.upload_url = "https://upload.vk.example/photo"
    api.photos.getWallUploadServer.return_value = server

    photo = AsyncMock()
    photo.owner_id = -100
    photo.id = 555  # id ФОТО — не должен попасть в квитанцию
    api.photos.saveWallPhoto.return_value = [photo]

    posted = AsyncMock()
    posted.post_id = 777  # post_id — вот это и есть квитанция
    api.wall.post.return_value = posted

    return api


@pytest.fixture
def fake_upload() -> AsyncMock:
    """Мок POST файла на upload-сервер -> {server, photo, hash}."""
    return AsyncMock(return_value={"server": 1, "photo": "[{}]", "hash": "deadbeef"})


@pytest.mark.asyncio
async def test_skips_when_already_published(
    store, publication_id, sample_post, fake_vk_api, fake_upload
):
    """Если (publication_id, channel) уже done — не публикуем повторно (без дублей)."""
    store.mark_done(publication_id, "vk", external_id="999")
    adapter = VKAdapter(fake_vk_api, target="-100", store=store, upload_photo=fake_upload)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    fake_upload.assert_not_called()
    fake_vk_api.wall.post.assert_not_called()


@pytest.mark.asyncio
async def test_publishes_and_returns_receipt(
    store, publication_id, sample_post, fake_vk_api, fake_upload
):
    """RED: первая публикация проходит все 4 шага и возвращает post_id как квитанцию."""
    adapter = VKAdapter(fake_vk_api, target="-100", store=store, upload_photo=fake_upload)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "777"  # квитанция — post_id из wall.post, не id фото

    # многошаговая загрузка отработала по разу каждым шагом
    fake_vk_api.photos.getWallUploadServer.assert_called_once()
    fake_upload.assert_called_once()
    fake_vk_api.photos.saveWallPhoto.assert_called_once()
    fake_vk_api.wall.post.assert_called_once()

    assert store.is_done(publication_id, "vk")  # помечено done
