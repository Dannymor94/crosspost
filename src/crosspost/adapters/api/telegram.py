"""Telegram-адаптер (Telethon, userbot). Эпик 2.

Постинг ОТ ЛИЦА КАНАЛА. userbot (не Bot API) — из-за транспорта MTProto в РФ.
Идемпотентность: пропустить, если (publication_id, channel) уже done.

Отправка parse_mode="html" — HTML-разметка (<b>, <a href>) рендерится как надо, и в
лимит начинает считаться видимый текст, а не сырая строка с тегами.

Два режима по длине видимого текста (лимит подписи 1024):
  влезает    → одно сообщение: фото + подпись;
  не влезает → фото с пустой подписью, следом отдельное сообщение с полным текстом.
  Тело > 4096 (одного сообщения мало) — FAILED, чанкинг сознательно не поддержан:
  риск дубля части текста в публичном канале дороже поддержки чанков.

Идемпотентность двухсообщённого режима (одна публикация = два вызова API):
  Факт «фото ушло» фиксируется в сторе под ВСПОМОГАТЕЛЬНЫМ ключом
  (f"{publication_id}#photo", "telegram"): channel остаётся реальным, суффикс висит
  на publication_id (непрозрачный коррелятор, не перечисление). Реальный ключ
  (publication_id, "telegram") помечается done ТОЛЬКО после обоих сообщений —
  is_done остаётся честным для оркестратора. При падении второго вызова возвращается
  FAILED; ретрай видит чекпоинт фото и досылает только текст, фото повторно не летит.
  Дедуп — по внутреннему publication_id, external_id в нём не участвует (§6.1).

Границу API↔браузер не нарушаем: сессионная логика (StringSession, start) живёт
в CLI/фабрике, сюда приходит готовый клиент.
"""

from __future__ import annotations

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.content.canonical import CanonicalContent
from crosspost.content.telegram_text import (
    MESSAGE_LIMIT,
    caption_fits,
    exceeds_message_limit,
)
from crosspost.orchestrator.task import IdempotencyStore

_PHOTO_STEP = "#photo"  # суффикс publication_id для чекпоинта «фото ушло»


def _first_id(sent) -> str:
    """id первого сообщения. send_file для альбома возвращает список; caption и
    шаринг-id несёт первое сообщение."""
    msg = sent[0] if isinstance(sent, list) else sent
    return str(msg.id)


class TelegramAdapter:
    channel = "telegram"

    def __init__(self, client, target: str, store: IdempotencyStore) -> None:
        self._client = client  # Telethon TelegramClient
        self._target = target  # канал назначения
        self._store = store

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult:
        # ── идемпотентность: вся публикация в канал уже прошла ────────────────
        if self._store.is_done(publication_id, self.channel):
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        # Telethon-исключения импортируем лениво (SDK не тянем на импорт модуля).
        from telethon.errors import FloodWaitError, RPCError
        from telethon.errors.rpcerrorlist import MediaCaptionTooLongError

        text = content.text or ""
        media = content.media_paths

        try:
            if caption_fits(text):
                # ── режим «фото + подпись»: один вызов ─────────────────────────
                sent = await self._client.send_file(
                    self._target, media, caption=text, parse_mode="html"
                )
                ext = _first_id(sent)
                self._store.mark_done(publication_id, self.channel, external_id=ext)
                return ChannelResult(self.channel, ResultStatus.DONE, external_id=ext)

            # ── режим «фото, затем текст следом»: два вызова ───────────────────
            if exceeds_message_limit(text):
                return ChannelResult(
                    self.channel,
                    ResultStatus.FAILED,
                    error=f"текст длиннее {MESSAGE_LIMIT} симв.: одного сообщения мало, "
                    "чанкинг не поддержан — сократите текст",
                )

            photo_pub = publication_id + _PHOTO_STEP

            # Шаг 1 — фото с пустой подписью. Чекпоинт переживает падение шага 2:
            # на ретрае фото не пересылается, id берём из стора.
            if self._store.is_done(photo_pub, self.channel):
                photo_id = self._store.get_external_id(photo_pub, self.channel)
            else:
                sent = await self._client.send_file(
                    self._target, media, caption="", parse_mode="html"
                )
                photo_id = _first_id(sent)
                self._store.mark_done(photo_pub, self.channel, external_id=photo_id)

            # Шаг 2 — полный текст отдельным сообщением.
            await self._client.send_message(self._target, text, parse_mode="html")

            # Оба сообщения прошли → канал done. Квитанция — id фото (визуальный якорь).
            self._store.mark_done(publication_id, self.channel, external_id=photo_id)
            return ChannelResult(self.channel, ResultStatus.DONE, external_id=photo_id)

        except FloodWaitError as e:
            # Время ожидания — в error, чтобы ретрай не пришёл раньше срока (§6.3).
            return ChannelResult(
                self.channel,
                ResultStatus.FAILED,
                error=f"flood_wait: подождать {e.seconds}s перед повтором",
            )
        except MediaCaptionTooLongError as e:
            return ChannelResult(self.channel, ResultStatus.FAILED, error=f"caption too long: {e}")
        except RPCError as e:
            # Частичный успех — норма (§6.8): не роняем публикацию, возвращаем FAILED.
            return ChannelResult(self.channel, ResultStatus.FAILED, error=str(e))


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = TelegramAdapter  # noqa: E305
