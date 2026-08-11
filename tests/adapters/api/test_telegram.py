"""Тесты TG-адаптера: parse_mode, два режима по длине, ошибки → FAILED,
идемпотентность двухсообщённого режима, экранирование HTML.

Всё на моках Telethon — ключи площадки не нужны.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crosspost.adapters.api.telegram import TelegramAdapter
from crosspost.adapters.base import ResultStatus
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.content.telegram_text import CAPTION_LIMIT, MESSAGE_LIMIT, escape_html


class _Req:
    """Заглушка request для конструктора Telethon-исключений."""


def _post(tmp_path: Path, text: str) -> CanonicalContent:
    img = tmp_path / "img.jpg"
    img.write_bytes(b"\xff\xd8\xff")
    return CanonicalContent(type=ContentType.POST, text=text, media_paths=[img])


# ── идемпотентный пропуск (было зелёным и осталось) ───────────────────────────


@pytest.mark.asyncio
async def test_skips_when_already_published(
    store, publication_id, sample_post, fake_telethon_client
):
    store.mark_done(publication_id, "telegram", external_id="999")
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    fake_telethon_client.send_file.assert_not_called()


@pytest.mark.asyncio
async def test_publishes_and_returns_receipt(
    store, publication_id, sample_post, fake_telethon_client
):
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "12345"
    fake_telethon_client.send_file.assert_called_once()
    assert store.is_done(publication_id, "telegram")


@pytest.mark.asyncio
async def test_public_username_yields_post_url(
    store, publication_id, sample_post, fake_telethon_client
):
    """Публичный @username → временный пермалинк t.me/<name>/<msg_id> в result.url."""
    adapter = TelegramAdapter(fake_telethon_client, target="@medithou", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.url == "https://t.me/medithou/12345"


@pytest.mark.asyncio
async def test_numeric_target_has_no_url(store, publication_id, sample_post, fake_telethon_client):
    """Числовой chat_id — публичной ссылки нет (не выдумываем) → url=None."""
    adapter = TelegramAdapter(fake_telethon_client, target="-1001234567890", store=store)

    result = await adapter.publish(sample_post, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.url is None


@pytest.mark.asyncio
async def test_text_only_uses_send_message_not_send_file(
    tmp_path, store, publication_id, fake_telethon_client
):
    """Пост без медиа → одно текстовое сообщение через send_message, не send_file."""
    sent = _Req()  # объект с .id — send_message возвращает сообщение
    sent.id = 777
    fake_telethon_client.send_message.return_value = sent

    content = CanonicalContent(type=ContentType.POST, text="просто текст", media_paths=[])
    adapter = TelegramAdapter(fake_telethon_client, target="@medithou", store=store)

    result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "777"
    assert result.url == "https://t.me/medithou/777"
    fake_telethon_client.send_file.assert_not_called()
    _, kwargs = fake_telethon_client.send_message.call_args
    assert kwargs["parse_mode"] == "html"
    assert store.is_done(publication_id, "telegram")


@pytest.mark.asyncio
async def test_text_only_over_limit_fails(tmp_path, store, publication_id, fake_telethon_client):
    """Текст без медиа длиннее MESSAGE_LIMIT → FAILED, ничего не отправляется."""
    content = CanonicalContent(
        type=ContentType.POST, text="я" * (MESSAGE_LIMIT + 1), media_paths=[]
    )
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.FAILED
    assert str(MESSAGE_LIMIT) in result.error
    fake_telethon_client.send_message.assert_not_called()
    fake_telethon_client.send_file.assert_not_called()
    assert not store.is_done(publication_id, "telegram")


@pytest.mark.asyncio
async def test_publishes_album_and_returns_first_message_id(
    store, publication_id, sample_album, fake_telethon_client_album
):
    adapter = TelegramAdapter(fake_telethon_client_album, target="@x", store=store)

    result = await adapter.publish(sample_album, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "100"
    fake_telethon_client_album.send_file.assert_called_once()
    assert store.is_done(publication_id, "telegram")


# ── 1) parse_mode="html" передан в send_file ──────────────────────────────────


@pytest.mark.asyncio
async def test_send_file_gets_parse_mode_html(
    store, publication_id, sample_post, fake_telethon_client
):
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    await adapter.publish(sample_post, publication_id=publication_id)

    _, kwargs = fake_telethon_client.send_file.call_args
    assert kwargs["parse_mode"] == "html"


# ── 2) подпись < 1024 → один вызов, режим «фото + подпись» ────────────────────


@pytest.mark.asyncio
async def test_short_caption_single_call(tmp_path, store, publication_id, fake_telethon_client):
    content = _post(tmp_path, "Короткая афиша дня")
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    fake_telethon_client.send_file.assert_called_once()
    fake_telethon_client.send_message.assert_not_called()
    # текст ушёл именно подписью
    _, kwargs = fake_telethon_client.send_file.call_args
    assert kwargs["caption"] == "Короткая афиша дня"


# ── 3) подпись > 1024 → два вызова, режим «фото + текст следом» ───────────────


@pytest.mark.asyncio
async def test_long_text_two_calls_photo_then_message(
    tmp_path, store, publication_id, fake_telethon_client
):
    # без переводов строки — заголовка нет, подпись пустая, тело = весь текст
    long_text = "я" * (CAPTION_LIMIT + 200)  # видимых > 1024, но < 4096
    content = _post(tmp_path, long_text)
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == "12345"  # квитанция — id фото
    # фото с пустой подписью (заголовок не выделился)…
    fake_telethon_client.send_file.assert_called_once()
    _, f_kwargs = fake_telethon_client.send_file.call_args
    assert f_kwargs["caption"] == ""
    assert f_kwargs["parse_mode"] == "html"
    # …и полный текст отдельным сообщением с parse_mode=html
    fake_telethon_client.send_message.assert_called_once()
    m_args, m_kwargs = fake_telethon_client.send_message.call_args
    assert long_text in m_args
    assert m_kwargs["parse_mode"] == "html"
    assert store.is_done(publication_id, "telegram")


@pytest.mark.asyncio
async def test_long_text_first_line_becomes_caption(
    tmp_path, store, publication_id, fake_telethon_client
):
    """Первая строка → подпись под фото, тело (без неё) → отдельным сообщением."""
    headline = "Афиша месяца — август"
    body = "тело поста " * 120  # так, чтобы весь текст был > 1024 видимых
    content = _post(tmp_path, f"{headline}\n{body}")
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    # фото получает заголовок подписью…
    _, f_kwargs = fake_telethon_client.send_file.call_args
    assert f_kwargs["caption"] == headline
    # …а тело идёт следом БЕЗ заголовка (без дубля)
    m_args, _ = fake_telethon_client.send_message.call_args
    sent_body = m_args[-1]
    assert sent_body == body
    assert headline not in sent_body


@pytest.mark.asyncio
async def test_body_over_message_limit_fails(tmp_path, store, publication_id, fake_telethon_client):
    """Тело > 4096 — FAILED, ничего не отправляется (чанкинг не поддержан)."""
    content = _post(tmp_path, "я" * (MESSAGE_LIMIT + 1))
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.FAILED
    assert str(MESSAGE_LIMIT) in result.error
    fake_telethon_client.send_file.assert_not_called()
    fake_telethon_client.send_message.assert_not_called()
    assert not store.is_done(publication_id, "telegram")


# ── 4) ошибка отправки → ChannelResult(FAILED), не исключение ─────────────────


@pytest.mark.asyncio
async def test_rpc_error_returns_failed_not_raise(
    tmp_path, store, publication_id, fake_telethon_client
):
    from telethon.errors import RPCError

    fake_telethon_client.send_file.side_effect = RPCError(_Req(), "boom")
    content = _post(tmp_path, "Афиша дня")
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.FAILED
    assert result.error
    assert not store.is_done(publication_id, "telegram")


@pytest.mark.asyncio
async def test_flood_wait_puts_seconds_in_error(
    tmp_path, store, publication_id, fake_telethon_client
):
    """§6.3: время ожидания FloodWait — в error, чтобы ретрай не пришёл раньше."""
    from telethon.errors import FloodWaitError

    err = FloodWaitError(_Req())
    fake_telethon_client.send_file.side_effect = err
    content = _post(tmp_path, "Афиша дня")
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.FAILED
    assert "flood_wait" in result.error
    assert f"{err.seconds}s" in result.error


# ── 5) ретрай после падения второго вызова не пересылает фото ─────────────────


@pytest.mark.asyncio
async def test_retry_after_message_failure_does_not_resend_photo(
    tmp_path, store, publication_id, fake_telethon_client
):
    from telethon.errors import FloodWaitError

    long_text = "я" * (CAPTION_LIMIT + 200)
    content = _post(tmp_path, long_text)
    adapter = TelegramAdapter(fake_telethon_client, target="@x", store=store)

    # Первый прогон: фото уходит, текст падает.
    fake_telethon_client.send_message.side_effect = [FloodWaitError(_Req()), None]

    r1 = await adapter.publish(content, publication_id=publication_id)
    assert r1.status is ResultStatus.FAILED
    assert fake_telethon_client.send_file.call_count == 1
    # чекпоинт фото зафиксирован под вспомогательным ключом, реальный ключ — нет
    assert store.is_done(publication_id + "#photo", "telegram")
    assert not store.is_done(publication_id, "telegram")

    # Ретрай тем же publication_id: фото НЕ пересылается, досылается только текст.
    r2 = await adapter.publish(content, publication_id=publication_id)
    assert r2.status is ResultStatus.DONE
    assert r2.external_id == "12345"
    assert fake_telethon_client.send_file.call_count == 1  # без повторной отправки фото
    assert fake_telethon_client.send_message.call_count == 2
    assert store.is_done(publication_id, "telegram")


# ── 6) текст с < > & не ломает разметку ──────────────────────────────────────


def test_escape_html_makes_valid_markup():
    """escape_html видимого фрагмента → собранный HTML корректно парсится Telegram."""
    from telethon.extensions import html as tg_html

    title = "Йога & дыхание <практика №1>"
    built = f"<b>{escape_html(title)}</b>"

    # экранированные спецсимволы — в виде сущностей, теги не сломаны
    assert "&amp;" in built and "&lt;" in built and "&gt;" in built

    # HTML-парсер Telegram принимает строку и возвращает исходный видимый текст
    visible, entities = tg_html.parse(built)
    assert visible == title  # &amp;/&lt;/&gt; раскодировались обратно
    assert [type(e).__name__ for e in entities] == ["MessageEntityBold"]


def test_escaping_prevents_tag_injection():
    """Вложенный в текст тег без экранирования срабатывает как разметка; с ним — нет."""
    from telethon.extensions import html as tg_html

    injected = "<i>x</i>"  # придёт из названия события — не должен стать курсивом

    raw_visible, raw_ents = tg_html.parse(f"<b>{injected}</b>")
    esc_visible, esc_ents = tg_html.parse(f"<b>{escape_html(injected)}</b>")

    # без экранирования <i> исполнился: видимый текст схлопнулся до "x", лишняя entity
    assert raw_visible == "x"
    assert "MessageEntityItalic" in [type(e).__name__ for e in raw_ents]
    # с экранированием тег остался видимым текстом, только Bold
    assert esc_visible == injected
    assert [type(e).__name__ for e in esc_ents] == ["MessageEntityBold"]
