"""Тесты ВК-адаптера.

Реальная форма vkbottle (сверено с исходниками):
  PhotoWallUploader(api).upload(path, group_id=...)         -> attachment str
  PhotoToAlbumUploader(api).upload(path, album_id=, group_id=) -> [photo_obj]
  api.wall.post(owner_id=, message=, attachments=)          -> obj с .post_id

vkbottle + vkbottle.exception_factory подменяются через sys.modules.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from crosspost.adapters.api.vk import VKAdapter, VKPhotoUploadError
from crosspost.adapters.base import ResultStatus

# ── фикстуры ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_vk_api() -> AsyncMock:
    api = AsyncMock()
    posted = AsyncMock()
    posted.post_id = 777
    api.wall.post.return_value = posted
    return api


@pytest.fixture
def fake_vkbottle(monkeypatch) -> types.ModuleType:
    """Happy path: PhotoWallUploader успешно загружает фото."""
    mod = types.ModuleType("vkbottle")
    exc_mod = types.ModuleType("vkbottle.exception_factory")

    # Базовый класс ошибок VK API — для isinstance-проверок в адаптере
    class VKAPIError(Exception):
        pass

    exc_mod.VKAPIError = VKAPIError

    wall_uploader = MagicMock(name="PhotoWallUploader()")
    wall_uploader.upload = AsyncMock(return_value="photo-100_555")
    mod.PhotoWallUploader = MagicMock(name="PhotoWallUploader", return_value=wall_uploader)

    album_uploader = MagicMock(name="PhotoToAlbumUploader()")
    album_uploader.upload = AsyncMock()  # не должен вызываться в happy path
    mod.PhotoToAlbumUploader = MagicMock(name="PhotoToAlbumUploader", return_value=album_uploader)

    monkeypatch.setitem(sys.modules, "vkbottle", mod)
    monkeypatch.setitem(sys.modules, "vkbottle.exception_factory", exc_mod)
    return mod


@pytest.fixture
def fake_vkbottle_wall_denied(monkeypatch) -> types.ModuleType:
    """Wall-загрузка → ACCESS_DENIED; альбомная загрузка успешна."""
    mod = types.ModuleType("vkbottle")
    exc_mod = types.ModuleType("vkbottle.exception_factory")

    class VKAPIError(Exception):
        pass

    exc_mod.VKAPIError = VKAPIError

    wall_uploader = MagicMock(name="PhotoWallUploader()")
    wall_uploader.upload = AsyncMock(side_effect=VKAPIError("ACCESS_DENIED (15)"))
    mod.PhotoWallUploader = MagicMock(name="PhotoWallUploader", return_value=wall_uploader)

    # upload() -> list[str] — готовые attachment-строки (не объекты фото)
    album_uploader = MagicMock(name="PhotoToAlbumUploader()")
    album_uploader.upload = AsyncMock(return_value=["photo-100_999"])
    mod.PhotoToAlbumUploader = MagicMock(name="PhotoToAlbumUploader", return_value=album_uploader)

    monkeypatch.setitem(sys.modules, "vkbottle", mod)
    monkeypatch.setitem(sys.modules, "vkbottle.exception_factory", exc_mod)
    return mod


@pytest.fixture
def fake_vkbottle_both_denied(monkeypatch) -> types.ModuleType:
    """Обе попытки загрузки → ACCESS_DENIED."""
    mod = types.ModuleType("vkbottle")
    exc_mod = types.ModuleType("vkbottle.exception_factory")

    class VKAPIError(Exception):
        pass

    exc_mod.VKAPIError = VKAPIError

    wall_uploader = MagicMock(name="PhotoWallUploader()")
    wall_uploader.upload = AsyncMock(side_effect=VKAPIError("ACCESS_DENIED wall"))
    mod.PhotoWallUploader = MagicMock(name="PhotoWallUploader", return_value=wall_uploader)

    album_uploader = MagicMock(name="PhotoToAlbumUploader()")
    album_uploader.upload = AsyncMock(side_effect=VKAPIError("ACCESS_DENIED album"))
    mod.PhotoToAlbumUploader = MagicMock(name="PhotoToAlbumUploader", return_value=album_uploader)

    monkeypatch.setitem(sys.modules, "vkbottle", mod)
    monkeypatch.setitem(sys.modules, "vkbottle.exception_factory", exc_mod)
    return mod


# ── тесты ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_when_already_published(
    store, publication_id, sample_post, fake_vk_api, fake_vkbottle
):
    store.mark_done(publication_id, "vk", external_id="999")
    adapter = VKAdapter(fake_vk_api, target="-100", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    fake_vkbottle.PhotoWallUploader.assert_not_called()
    fake_vk_api.wall.post.assert_not_called()


@pytest.mark.asyncio
async def test_publishes_via_wall_uploader(
    store, publication_id, sample_post, fake_vk_api, fake_vkbottle
):
    """Happy path: PhotoWallUploader (getWallUploadServer) проходит."""
    adapter = VKAdapter(fake_vk_api, target="-100", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "777"

    fake_vkbottle.PhotoWallUploader.assert_called_once_with(fake_vk_api)
    uploader = fake_vkbottle.PhotoWallUploader.return_value
    uploader.upload.assert_awaited_once()
    assert uploader.upload.call_args.kwargs["group_id"] == 100

    fake_vkbottle.PhotoToAlbumUploader.assert_not_called()  # вторая попытка не нужна

    post_kwargs = fake_vk_api.wall.post.call_args.kwargs
    assert post_kwargs["owner_id"] == -100
    assert post_kwargs["attachments"] == "photo-100_555"
    assert store.is_done(publication_id, "vk")


@pytest.mark.asyncio
async def test_falls_back_to_album_uploader_on_wall_denied(
    store, publication_id, sample_post, fake_vk_api, fake_vkbottle_wall_denied
):
    """Если getWallUploadServer → ACCESS_DENIED, пробуем PhotoToAlbumUploader."""
    mod = fake_vkbottle_wall_denied
    adapter = VKAdapter(fake_vk_api, target="-100", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "777"

    # wall-попытка была сделана (и упала)
    mod.PhotoWallUploader.assert_called_once_with(fake_vk_api)
    # album-попытка сработала
    mod.PhotoToAlbumUploader.assert_called_once_with(fake_vk_api)
    album_uploader = mod.PhotoToAlbumUploader.return_value
    album_uploader.upload.assert_awaited_once()
    call_args = album_uploader.upload.call_args
    # upload(album_id, paths_like, group_id=) — первые два позиционные
    assert call_args.args[0] == -100  # album_id = -group_id (wall album)
    assert call_args.kwargs["group_id"] == 100

    post_kwargs = fake_vk_api.wall.post.call_args.kwargs
    assert post_kwargs["attachments"] == "photo-100_999"
    assert store.is_done(publication_id, "vk")


@pytest.mark.asyncio
async def test_raises_explicit_error_when_both_methods_denied(
    store, publication_id, sample_post, fake_vk_api, fake_vkbottle_both_denied
):
    """Если обе попытки ACCESS_DENIED → VKPhotoUploadError с явным текстом."""
    adapter = VKAdapter(fake_vk_api, target="-100", store=store)

    with pytest.raises(VKPhotoUploadError) as exc_info:
        await adapter.publish(sample_post, publication_id=publication_id)

    msg = str(exc_info.value)
    assert "PhotoWallUploader" in msg
    assert "PhotoToAlbumUploader" in msg
    assert "VK_PHOTO_UPLOAD_ENABLED=false" in msg  # подсказка пользователю

    # wall.post не должен был вызываться — фото не загружено
    fake_vk_api.wall.post.assert_not_called()
    assert not store.is_done(publication_id, "vk")


@pytest.mark.asyncio
async def test_photo_upload_disabled_posts_text_only(
    store, publication_id, sample_post, fake_vk_api, fake_vkbottle
):
    """VK_PHOTO_UPLOAD_ENABLED=false: wall.post без attachments, SDK не вызывается."""
    adapter = VKAdapter(fake_vk_api, target="-100", store=store, photo_upload=False)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id is not None
    assert "photo_skipped" in result.external_id  # явная пометка в квитанции

    fake_vkbottle.PhotoWallUploader.assert_not_called()
    fake_vkbottle.PhotoToAlbumUploader.assert_not_called()

    post_kwargs = fake_vk_api.wall.post.call_args.kwargs
    assert "attachments" not in post_kwargs  # чистый текстовый пост
    assert store.is_done(publication_id, "vk")
