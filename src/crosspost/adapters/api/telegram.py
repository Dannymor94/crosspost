"""Telegram-адаптер (Telethon, userbot). Эпик 2 — [СТАРТ].

Постинг ОТ ЛИЦА КАНАЛА. userbot (не Bot API) — из-за транспорта MTProto в РФ.
Идемпотентность: пропустить, если (publication_id, channel) уже done.

Статус: КАРКАС. publish ещё не реализован — на это указывает первый красный тест
(tests/adapters/api/test_telegram.py). Реализовать в фазе A под TDD.
"""
from __future__ import annotations

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.content.canonical import CanonicalContent
from crosspost.orchestrator.task import IdempotencyStore


class TelegramAdapter:
    channel = "telegram"

    def __init__(self, client, target: str, store: IdempotencyStore) -> None:
        self._client = client          # Telethon TelegramClient
        self._target = target          # канал назначения
        self._store = store

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult:
        # 1) идемпотентность — дедуп по внутреннему ключу, не по external_id
        if self._store.is_done(publication_id, self.channel):
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        # 2) рендер под TG: текст поста становится подписью к медиа
        caption = content.text

        # 3) отправка медиа с подписью через Telethon (от лица канала)
        sent = await self._client.send_file(
            self._target,
            content.media_paths,
            caption=caption,
        )

        # 4) квитанция — message_id первого сообщения (у альбома send_file возвращает список;
        #    первое сообщение несёт caption и его id показывается при шаринге альбома).
        msg = sent[0] if isinstance(sent, list) else sent
        external_id = str(msg.id)
        self._store.mark_done(publication_id, self.channel, external_id=external_id)
        return ChannelResult(self.channel, ResultStatus.DONE, external_id=external_id)


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = TelegramAdapter  # noqa: E305
