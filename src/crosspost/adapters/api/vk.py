"""ВК-адаптер (VK API через vkbottle). Эпик 2.

Публикация на стену сообщества.

Загрузка фото — цепочка двух попыток (обе с токеном сообщества):
  1. PhotoWallUploader  — внутри вызывает photos.getWallUploadServer.
     Работает с user-токеном; с community-токеном VK может вернуть ACCESS_DENIED.
  2. PhotoAlbumUploader — внутри вызывает photos.getUploadServer (загрузка в альбом
     сообщества, group_id). Attachment берётся из первого сохранённого фото.
     Проверяем, доступен ли этот метод community-токену.

Если обе попытки дали VKAPIError → VKPhotoUploadError с явным текстом (не стек).
Если VK_PHOTO_UPLOAD_ENABLED=false → пропускаем фото, постим только текст.

Квитанция — post_id из wall.post.
Идемпотентность: пропустить если (publication_id, channel) уже done.
vkbottle импортируется ЛЕНИВО внутри publish.
"""
from __future__ import annotations

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.content.canonical import CanonicalContent
from crosspost.orchestrator.task import IdempotencyStore


class VKPhotoUploadError(Exception):
    """Ни один метод загрузки фото не прошёл с текущим токеном."""


class VKAdapter:
    channel = "vk"

    def __init__(
        self,
        api,
        target: str,
        store: IdempotencyStore,
        *,
        photo_upload: bool = True,
    ) -> None:
        self._api = api              # vkbottle API
        self._target = int(target)   # owner_id стены (сообщество: отрицательный)
        self._store = store
        self._photo_upload = photo_upload

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult:
        if self._store.is_done(publication_id, self.channel):
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        attachment: str | None = None
        if self._photo_upload and content.media_paths:
            attachment = await self._upload_photo(str(content.media_paths[0]))

        posted = await self._api.wall.post(
            owner_id=self._target,
            message=content.text,
            **({"attachments": attachment} if attachment else {}),
        )

        external_id = str(posted.post_id)
        self._store.mark_done(publication_id, self.channel, external_id=external_id)

        status = ResultStatus.DONE
        if not attachment and content.media_paths:
            # фото было, но загрузка отключена флагом — фиксируем в квитанции
            external_id += ":photo_skipped"
        return ChannelResult(self.channel, status, external_id=external_id)

    async def _upload_photo(self, path: str) -> str:
        """Загрузить фото, вернуть attachment-строку "photo{owner}_{id}".

        Попытка 1: PhotoWallUploader (photos.getWallUploadServer).
        Попытка 2: PhotoToAlbumUploader (photos.getUploadServer, album_id=-group_id).
        Обе — community-токеном. Если ни одна не прошла → VKPhotoUploadError.
        """
        from vkbottle import PhotoToAlbumUploader, PhotoWallUploader
        from vkbottle.exception_factory import VKAPIError

        group_id = abs(self._target)

        # Попытка 1 — getWallUploadServer
        try:
            uploader = PhotoWallUploader(self._api)
            return await uploader.upload(path, group_id=group_id)
        except VKAPIError as e:
            wall_error = f"PhotoWallUploader (getWallUploadServer): {e}"

        # Попытка 2 — getUploadServer (альбом стены сообщества, album_id=-group_id).
        # PhotoToAlbumUploader.upload(album_id, paths_like, group_id=) -> list[str]
        # где каждый элемент — готовая attachment-строка "photo{owner}_{id}".
        try:
            album_uploader = PhotoToAlbumUploader(self._api)
            attachments = await album_uploader.upload(
                -group_id,   # wall album id = -group_id
                path,
                group_id=group_id,
            )
            return attachments[0]
        except VKAPIError as e:
            album_error = f"PhotoToAlbumUploader (getUploadServer): {e}"

        raise VKPhotoUploadError(
            "Загрузка фото не прошла ни одним из методов community-токеном.\n"
            f"  1) {wall_error}\n"
            f"  2) {album_error}\n"  # type: ignore[possibly-undefined]
            "Варианты: установите VK_PHOTO_UPLOAD_ENABLED=false (текстовый пост) "
            "или переключитесь на user-токен с правами photos."
        )


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = VKAdapter  # noqa: E305
