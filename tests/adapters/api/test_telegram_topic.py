"""Tests for forum-topic support in TelegramAdapter — reply_to and peer resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from crosspost.adapters.api.telegram import TelegramAdapter
from crosspost.adapters.base import ResultStatus
from crosspost.content.canonical import CanonicalContent, ContentType


def _post(tmp_path: Path, text: str = "text") -> CanonicalContent:
    img = tmp_path / "img.jpg"
    img.write_bytes(b"\xff\xd8\xff")
    return CanonicalContent(type=ContentType.POST, text=text, media_paths=[img])


def _text_content(text: str = "hello") -> CanonicalContent:
    return CanonicalContent(type=ContentType.POST, text=text, media_paths=[])


def _long_caption(tmp_path: Path) -> CanonicalContent:
    """Post with caption > 1024 chars — triggers two-message mode."""
    img = tmp_path / "img.jpg"
    img.write_bytes(b"\xff\xd8\xff")
    headline = "Заголовок"
    body = "б" * 1100
    return CanonicalContent(type=ContentType.POST, text=f"{headline}\n{body}", media_paths=[img])


@pytest.fixture
def client():
    c = AsyncMock()
    sent = AsyncMock()
    sent.id = 42
    c.send_file.return_value = sent
    c.send_message.return_value = sent
    return c


# ── No topic_id → no reply_to ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_topic_no_reply_to_send_message(store, publication_id, client):
    adapter = TelegramAdapter(client, target="@x", store=store)
    content = _text_content()
    await adapter.publish(content, publication_id=publication_id)
    _, kw = client.send_message.call_args
    assert "reply_to" not in kw


@pytest.mark.asyncio
async def test_no_topic_no_reply_to_send_file(tmp_path, store, publication_id, client):
    adapter = TelegramAdapter(client, target="@x", store=store)
    await adapter.publish(_post(tmp_path), publication_id=publication_id)
    _, kw = client.send_file.call_args
    assert "reply_to" not in kw


# ── topic_id set → reply_to on all 4 send paths ───────────────────────────────


@pytest.mark.asyncio
async def test_topic_reply_to_text_only(store, publication_id, client):
    """Path A: text-only send_message gets reply_to=topic_id."""
    adapter = TelegramAdapter(client, target="@x", store=store, topic_id=285)
    await adapter.publish(_text_content(), publication_id=publication_id)
    _, kw = client.send_message.call_args
    assert kw["reply_to"] == 285


@pytest.mark.asyncio
async def test_topic_reply_to_media_caption(tmp_path, store, publication_id, client):
    """Path B: media+caption send_file gets reply_to=topic_id."""
    adapter = TelegramAdapter(client, target="@x", store=store, topic_id=285)
    await adapter.publish(_post(tmp_path), publication_id=publication_id)
    _, kw = client.send_file.call_args
    assert kw["reply_to"] == 285


@pytest.mark.asyncio
async def test_topic_reply_to_two_message_both_calls(tmp_path, store, publication_id, client):
    """Path C+D: two-message mode — both send_file and send_message get reply_to."""
    sent = AsyncMock()
    sent.id = 100
    client.send_file.return_value = sent

    adapter = TelegramAdapter(client, target="@x", store=store, topic_id=285)
    result = await adapter.publish(_long_caption(tmp_path), publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    _, sf_kw = client.send_file.call_args
    _, sm_kw = client.send_message.call_args
    assert sf_kw["reply_to"] == 285
    assert sm_kw["reply_to"] == 285


# ── Receipt includes topic_id ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_topic_id_in_receipt(tmp_path, store, publication_id, client):
    adapter = TelegramAdapter(client, target="@x", store=store, topic_id=285)
    result = await adapter.publish(_post(tmp_path), publication_id=publication_id)
    assert result.topic_id == 285


@pytest.mark.asyncio
async def test_no_topic_id_in_receipt_is_none(tmp_path, store, publication_id, client):
    adapter = TelegramAdapter(client, target="@x", store=store)
    result = await adapter.publish(_post(tmp_path), publication_id=publication_id)
    assert result.topic_id is None


# ── Peer resolution ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_peer_raw_string_when_no_access_hash(tmp_path, store, publication_id, client):
    """No access_hash → target string passed as-is."""
    adapter = TelegramAdapter(client, target="-1001234567890", store=store)
    await adapter.publish(_post(tmp_path), publication_id=publication_id)
    peer_arg = client.send_file.call_args.args[0]
    assert peer_arg == "-1001234567890"


@pytest.mark.asyncio
async def test_peer_input_peer_channel_when_access_hash_set(
    tmp_path, store, publication_id, client, monkeypatch
):
    """access_hash + -100<id> → InputPeerChannel passed as first arg."""
    from telethon.tl import types as tl_types

    adapter = TelegramAdapter(
        client,
        target="-1001234567890",
        store=store,
        peer_access_hash=809770730841868877,
    )
    await adapter.publish(_post(tmp_path), publication_id=publication_id)
    peer_arg = client.send_file.call_args.args[0]
    assert isinstance(peer_arg, tl_types.InputPeerChannel)
    assert peer_arg.channel_id == 1234567890
    assert peer_arg.access_hash == 809770730841868877


@pytest.mark.asyncio
async def test_peer_raw_string_username_with_access_hash(tmp_path, store, publication_id, client):
    """access_hash is ignored for @username targets (no -100 prefix)."""
    adapter = TelegramAdapter(
        client,
        target="@mygroup",
        store=store,
        peer_access_hash=12345,
    )
    await adapter.publish(_post(tmp_path), publication_id=publication_id)
    peer_arg = client.send_file.call_args.args[0]
    assert peer_arg == "@mygroup"
