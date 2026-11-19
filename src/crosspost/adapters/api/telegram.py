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
from typing import Any

from crosspost.adapters.base import ChannelAdapter, ChannelResult, FailureKind, ResultStatus
from crosspost.content.canonical import CanonicalContent
from crosspost.content.media_kind import MediaKind, detect_media_kind
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


def _video_meta(path) -> tuple[float, int, int]:
    """(длительность, ширина, высота) видео через hachoir — тот же разбор, что делает
    Telethon, когда атрибуты не заданы (telethon.utils._get_metadata). Без hachoir или
    при сбое разбора — нули: Telegram примет, но покажет 0:00 и квадратное превью."""
    try:
        from hachoir.core import config as hachoir_config
        from hachoir.metadata import extractMetadata
        from hachoir.parser import createParser
    except ImportError:
        return 0.0, 0, 0
    hachoir_config.quiet = True  # its parser warnings go to stdout, not to our logs
    try:
        parser = createParser(str(path))
        if parser is None:
            return 0.0, 0, 0
        with parser:
            meta = extractMetadata(parser)
    except Exception:  # noqa: BLE001 — битый/непонятный файл: публикуем с нулями
        return 0.0, 0, 0
    if meta is None:
        return 0.0, 0, 0
    dur = meta.get("duration").total_seconds() if meta.has("duration") else 0.0
    w = int(meta.get("width")) if meta.has("width") else 0
    h = int(meta.get("height")) if meta.has("height") else 0
    return dur, w, h


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


def _resolve_peer(target: str, peer_access_hash: int | None):
    """Return InputPeerChannel for -100<id> targets when access_hash is known, else raw string."""
    if peer_access_hash is not None and target.startswith("-100"):
        try:
            from telethon.tl import types as tl_types  # noqa: PLC0415

            channel_id = int(target[4:])  # strip -100 prefix
            return tl_types.InputPeerChannel(channel_id=channel_id, access_hash=peer_access_hash)
        except Exception:  # noqa: BLE001
            pass
    return target


class TelegramAdapter:
    channel = "telegram"

    def __init__(
        self,
        client,
        target: str,
        store: IdempotencyStore,
        *,
        schedule: datetime | None = None,
        topic_id: int | None = None,
        peer_access_hash: int | None = None,
    ) -> None:
        self._client = client  # Telethon TelegramClient
        self._target = target  # канал назначения
        self._store = store
        self._schedule = schedule
        self._topic_id = topic_id
        self._peer = _resolve_peer(target, peer_access_hash)

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult:
        # ── идемпотентность: вся публикация в канал уже прошла ────────────────
        if await self._store.is_done(publication_id, self.channel):
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        # Telethon-исключения импортируем лениво (SDK не тянем на импорт модуля).
        from telethon.errors import (
            AuthKeyUnregisteredError,
            FloodWaitError,
            RPCError,
            SessionRevokedError,
            UserDeactivatedBanError,
        )
        from telethon.errors.rpcerrorlist import MediaCaptionTooLongError
        from telethon.tl.types import DocumentAttributeVideo

        text = content.text or ""
        media = content.media_paths

        # Video: DocumentAttributeVideo with supports_streaming (plays at once). Explicit
        # attributes override the ones Telethon would infer, so the real duration and
        # size are read here (hachoir); zeros only when the file cannot be parsed —
        # Telegram then shows 0:00 and a square preview.
        video_attrs: list | None = None
        if media and detect_media_kind(media[0]) == MediaKind.VIDEO:
            dur, w, h = _video_meta(media[0])
            video_attrs = [DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)]

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
                kw: dict = {}
                if self._topic_id is not None:
                    kw["reply_to"] = self._topic_id
                sent = await self._client.send_message(
                    self._peer, text, parse_mode="html", schedule=self._schedule, **kw
                )
                ext = _first_id(sent)
                await self._store.mark_done(publication_id, self.channel, external_id=ext)
                return ChannelResult(
                    self.channel,
                    ResultStatus.DONE,
                    external_id=ext,
                    url=_post_url(self._target, ext),
                    topic_id=self._topic_id,
                )

            if caption_fits(text):
                # ── режим «медиа + подпись»: один вызов ────────────────────────
                extra: dict[str, Any] = {"attributes": video_attrs} if video_attrs else {}
                if self._topic_id is not None:
                    extra["reply_to"] = self._topic_id
                sent = await self._client.send_file(
                    self._peer,
                    media,
                    caption=text,
                    parse_mode="html",
                    schedule=self._schedule,
                    **extra,
                )
                ext = _first_id(sent)
                await self._store.mark_done(publication_id, self.channel, external_id=ext)
                return ChannelResult(
                    self.channel,
                    ResultStatus.DONE,
                    external_id=ext,
                    url=_post_url(self._target, ext),
                    topic_id=self._topic_id,
                )

            # ── режим «медиа, затем текст следом»: два вызова ──────────────────
            # Первая строка → подпись под медиа (чтобы не висело немым), остальное —
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

            # Шаг 1 — медиа с короткой подписью-заголовком. Чекпоинт переживает
            # падение шага 2: на ретрае медиа не пересылается, id берём из стора.
            if await self._store.is_done(photo_pub, self.channel):
                photo_id = await self._store.get_external_id(photo_pub, self.channel)
            else:
                extra2: dict[str, Any] = {"attributes": video_attrs} if video_attrs else {}
                if self._topic_id is not None:
                    extra2["reply_to"] = self._topic_id
                sent = await self._client.send_file(
                    self._peer,
                    media,
                    caption=caption,
                    parse_mode="html",
                    schedule=self._schedule,
                    **extra2,
                )
                photo_id = _first_id(sent)
                await self._store.mark_done(photo_pub, self.channel, external_id=photo_id)

            # Шаг 2 — тело (без заголовка) отдельным сообщением, на то же время.
            kw2: dict = {}
            if self._topic_id is not None:
                kw2["reply_to"] = self._topic_id
            await self._client.send_message(
                self._peer, body, parse_mode="html", schedule=self._schedule, **kw2
            )

            # Оба сообщения прошли → канал done. Квитанция — id фото (визуальный якорь).
            await self._store.mark_done(publication_id, self.channel, external_id=photo_id)
            return ChannelResult(
                self.channel,
                ResultStatus.DONE,
                external_id=photo_id,
                url=_post_url(self._target, photo_id),
                topic_id=self._topic_id,
            )

        except FloodWaitError as e:
            # Время ожидания — в error, чтобы ретрай не пришёл раньше срока (§6.3).
            return ChannelResult(
                self.channel,
                ResultStatus.FAILED,
                error="Лимит Telegram",
                failure_kind=FailureKind.RATE_LIMIT,
                retry_after=e.seconds,
            )
        except MediaCaptionTooLongError as e:
            return ChannelResult(self.channel, ResultStatus.FAILED, error=f"caption too long: {e}")
        except UserDeactivatedBanError:
            return ChannelResult(
                self.channel,
                ResultStatus.FAILED,
                error="Аккаунт заблокирован",
                failure_kind=FailureKind.BANNED,
            )
        except (AuthKeyUnregisteredError, SessionRevokedError):
            return ChannelResult(
                self.channel,
                ResultStatus.NEEDS_RELOGIN,
                error="Требуется вход",
                failure_kind=FailureKind.AUTH,
            )
        except RPCError as e:
            # Частичный успех — норма (§6.8): не роняем публикацию, возвращаем FAILED.
            return ChannelResult(self.channel, ResultStatus.FAILED, error=str(e))


# проверка соответствия контракту на этапе импорта-тайпчека
_: type[ChannelAdapter] = TelegramAdapter  # noqa: E305
