"""ВК-адаптер (VK API через vkbottle). Эпик 2.

Публикация на стену сообщества:
  PhotoWallUploader(api).upload(path, group_id=...) -> "photo{owner_id}_{id}"
  (uploader сам проходит photos.getWallUploadServer -> POST файла ->
   photos.saveWallPhoto и отдаёт готовую attachment-строку),
  затем api.wall.post(...) -> post_id. Квитанция — post_id (НЕ id фото).

Идемпотентность: пропустить, если (publication_id, channel) уже done.
vkbottle импортируется ЛЕНИВО внутри publish (как и инъекция клиента в
telegram.py — тяжёлый SDK не тащим в шапку, тесты остаются лёгкими).
"""
from __future__ import annotations

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.content.canonical import CanonicalContent
from crosspost.orchestrator.task import IdempotencyStore


class VKAdapter:
    channel = "vk"

    def __init__(self, api, target: str, store: IdempotencyStore) -> None:
        self._api = api              # vkbottle API (инжектится; реальный строится в build_adapter)
        self._target = int(target)   # owner_id стены (сообщество: отрицательный)
        self._store = store

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult:
        # идемпотентность — дедуп по внутреннему ключу, не по external_id
        if self._store.is_done(publication_id, self.channel):
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        from vkbottle import PhotoWallUploader  # ленивый импорт SDK

        # 1) загрузка фото: uploader сам делает многошаговый upload и
        #    возвращает готовую attachment-строку "photo{owner_id}_{id}"
        uploader = PhotoWallUploader(self._api)
        attachment = await uploader.upload(
            str(content.media_paths[0]),
            group_id=abs(self._target),
        )

        # 2) публикация записи -> post_id (квитанция, НЕ id фото)
        posted = await self._api.wall.post(
            owner_id=self._target,
            message=content.text,
            attachments=attachment,
        )

        external_id = str(posted.post_id)
        self._store.mark_done(publication_id, self.channel, external_id=external_id)
        return ChannelResult(self.channel, ResultStatus.DONE, external_id=external_id)


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = VKAdapter  # noqa: E305
