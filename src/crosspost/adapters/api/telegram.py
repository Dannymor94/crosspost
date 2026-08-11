"""Telegram-адаптер (Telethon, userbot). Эпик 2.

Постинг ОТ ЛИЦА КАНАЛА. userbot (не Bot API) — из-за транспорта MTProto в РФ.
Идемпотентность: пропустить, если (publication_id, channel) уже done.

Отправка parse_mode="html" — HTML-разметка (<b>, <a href>) рендерится как надо, и в
лимит начинает считаться видимый текст, а не сырая строка с тегами.

Два режима по длине видимого текста (лимит подписи 1024):
  влезает    → одно сообщение: фото + подпись;
  не влезает → фото с подписью-заголовком (первая строка поста, чтобы не висело
               немым), следом отдельное сообщение с телом (без заголовка).
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

Отложенная публикация: конструктор принимает schedule (aware datetime). None →
отправить сразу; datetime → нативный отложенный пост Telegram (schedule= во всех
send_file/send_message). Владелец правит/удаляет его в «Отложенных» канала;
квитанция external_id = id отложенного сообщения (у отложенных своё пространство id).

Границу API↔браузер не нарушаем: сессионная логика (StringSession, start) живёт
в CLI/фабрике, сюда приходит готовый клиент.
"""

from __future__ import annotations

import re
from datetime import datetime

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.content.canonical import CanonicalContent
from crosspost.content.telegram_text import (
    MESSAGE_LIMIT,
    caption_fits,
    exceeds_message_limit,
    split_headline,
)
from crosspost.orchestrator.task import IdempotencyStore

_PHOTO_STEP = "#photo"  # суффикс publication_id для чекпоинта «фото ушло»

# Публичный username канала: буквы/цифры/подчёркивания, начинается с буквы.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def _first_id(sent) -> str:
    """id первого сообщения. send_file для альбома возвращает список; caption и
    шаринг-id несёт первое сообщение."""
    msg = sent[0] if isinstance(sent, list) else sent
    return str(msg.id)


def _post_url(target: str, msg_id: str | None) -> str | None:
    """Пермалинк поста t.me/<name>/<id> — только для ПУБЛИЧНОГО канала (@username).

    Для числового chat_id / приватного инвайта публичной ссылки нет → None
    (ссылку не выдумываем — §«не угадывать»). Нет msg_id → None.
    """
    if not msg_id:
        return None
    name = target.lstrip("@").removeprefix("https://t.me/").removeprefix("t.me/").strip("/")
    return f"https://t.me/{name}/{msg_id}" if _USERNAME_RE.match(name) else None


class TelegramAdapter:
    channel = "telegram"

    def __init__(
        self, client, target: str, store: IdempotencyStore, *, schedule: datetime | None = None
    ) -> None:
        self._client = client  # Telethon TelegramClient
        self._target = target  # канал назначения
        self._store = store
        # None → отправить сразу; aware datetime → нативный отложенный пост Telegram
        # (schedule= в Telethon). При двух сообщениях ОБА на одно время: фото создаётся
        # первым → срабатывает первым (проверено), в ленте окажется над текстом.
        self._schedule = schedule

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
            if not media:
                # ── текст без медиа: одно текстовое сообщение (send_message) ────
                # send_file без файла в Telethon невалиден — для text-only шлём
                # обычное сообщение. Лимит — MESSAGE_LIMIT (чанкинг не поддержан).
                if exceeds_message_limit(text):
                    return ChannelResult(
                        self.channel,
                        ResultStatus.FAILED,
                        error=f"текст длиннее {MESSAGE_LIMIT} симв.: одного сообщения мало, "
                        "чанкинг не поддержан — сократите текст",
                    )
                sent = await self._client.send_message(
                    self._target, text, parse_mode="html", schedule=self._schedule
                )
                ext = _first_id(sent)
                self._store.mark_done(publication_id, self.channel, external_id=ext)
                return ChannelResult(
                    self.channel,
                    ResultStatus.DONE,
                    external_id=ext,
                    url=_post_url(self._target, ext),
                )

            if caption_fits(text):
                # ── режим «фото + подпись»: один вызов ─────────────────────────
                sent = await self._client.send_file(
                    self._target, media, caption=text, parse_mode="html", schedule=self._schedule
                )
                ext = _first_id(sent)
                self._store.mark_done(publication_id, self.channel, external_id=ext)
                return ChannelResult(
                    self.channel,
                    ResultStatus.DONE,
                    external_id=ext,
                    url=_post_url(self._target, ext),
                )

            # ── режим «фото, затем текст следом»: два вызова ───────────────────
            # Первая строка → подпись под фото (чтобы не висело немым), остальное —
            # отдельным сообщением. Тело проверяем на лимит сообщения.
            caption, body = split_headline(text)
            if exceeds_message_limit(body):
                return ChannelResult(
                    self.channel,
                    ResultStatus.FAILED,
                    error=f"текст длиннее {MESSAGE_LIMIT} симв.: одного сообщения мало, "
                    "чанкинг не поддержан — сократите текст",
                )

            photo_pub = publication_id + _PHOTO_STEP

            # Шаг 1 — фото с короткой подписью-заголовком. Чекпоинт переживает
            # падение шага 2: на ретрае фото не пересылается, id берём из стора.
            if self._store.is_done(photo_pub, self.channel):
                photo_id = self._store.get_external_id(photo_pub, self.channel)
            else:
                sent = await self._client.send_file(
                    self._target, media, caption=caption, parse_mode="html", schedule=self._schedule
                )
                photo_id = _first_id(sent)
                self._store.mark_done(photo_pub, self.channel, external_id=photo_id)

            # Шаг 2 — тело (без заголовка) отдельным сообщением, на то же время.
            await self._client.send_message(
                self._target, body, parse_mode="html", schedule=self._schedule
            )

            # Оба сообщения прошли → канал done. Квитанция — id фото (визуальный якорь).
            self._store.mark_done(publication_id, self.channel, external_id=photo_id)
            return ChannelResult(
                self.channel,
                ResultStatus.DONE,
                external_id=photo_id,
                url=_post_url(self._target, photo_id),
            )

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
