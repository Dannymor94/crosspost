"""ВК-адаптер (VK API). Эпик 2. wall.post + многошаговая загрузка фото.

Публикация на стену: photos.getWallUploadServer -> POST файла на upload-сервер
-> photos.saveWallPhoto -> wall.post. Квитанция — post_id из wall.post.

Идемпотентность: пропустить, если (publication_id, channel) уже done.
SDK/HTTP импортируются ЛЕНИВО внутри метода (как Telethon в telegram.py),
чтобы импорт модуля/тестов оставался лёгким.

Статус: КАРКАС. publish ещё не реализован (красный тест test_vk.py).
"""
from __future__ import annotations

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.content.canonical import CanonicalContent
from crosspost.orchestrator.task import IdempotencyStore


class VKAdapter:
    channel = "vk"

    def __init__(self, api, target: str, store: IdempotencyStore, *, upload_photo=None) -> None:
        self._api = api              # VK API client (mockable); реальный — vkbottle, импорт отложен
        self._target = int(target)   # owner_id стены (группа: отрицательный)
        self._store = store
        # callable(upload_url, path) -> {server, photo, hash}; по умолчанию httpx (ленивый импорт)
        self._upload_photo = upload_photo

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult:
        # идемпотентность — дедуп по внутреннему ключу, не по external_id
        if self._store.is_done(publication_id, self.channel):
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        # 1) получить адрес upload-сервера (группа: group_id положительный)
        server = await self._api.photos.getWallUploadServer(group_id=abs(self._target))

        # 2) залить файл на upload-сервер -> {server, photo, hash}
        uploaded = await self._upload(server.upload_url, content.media_paths[0])

        # 3) сохранить фото на стене -> объект с owner_id/id для attachment
        saved = await self._api.photos.saveWallPhoto(
            server=uploaded["server"],
            photo=uploaded["photo"],
            hash=uploaded["hash"],
        )
        photo = saved[0]
        attachment = f"photo{photo.owner_id}_{photo.id}"

        # 4) опубликовать запись -> post_id (квитанция, НЕ id фото)
        posted = await self._api.wall.post(
            owner_id=self._target,
            message=content.text,
            attachments=attachment,
        )

        external_id = str(posted.post_id)
        self._store.mark_done(publication_id, self.channel, external_id=external_id)
        return ChannelResult(self.channel, ResultStatus.DONE, external_id=external_id)

    async def _upload(self, upload_url: str, path) -> dict:
        """POST файла на upload-сервер ВК. httpx импортируется лениво (тесты лёгкие)."""
        if self._upload_photo is not None:
            return await self._upload_photo(upload_url, path)
        import httpx

        async with httpx.AsyncClient() as http:
            with open(path, "rb") as f:
                resp = await http.post(upload_url, files={"photo": f})
        return resp.json()


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = VKAdapter  # noqa: E305
