"""Video support in Telegram adapter — increment 1.

Tests:
1. Video post to Telegram publishes with DocumentAttributeVideo (supports_streaming=True).
2. Video post targeting a video-incapable channel → rejected at eligibility with explicit reason.
3. Oversized video download → MediaSourceError with explicit reason, nothing stored.
4. stream_to_file writes to disk incrementally; SSRF guard fires before any download.
5. Media kind is distinguished deterministically by extension; image/video paths behave differently.
6. Existing photo posting unchanged (regression).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from crosspost.adapters.api.telegram import TelegramAdapter
from crosspost.adapters.base import ResultStatus
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.content.capabilities import supports_video
from crosspost.content.media_kind import MediaKind, detect_media_kind
from crosspost.orchestrator.task import InMemoryIdempotencyStore, new_publication_id
from crosspost.scheduler.media_sources import MediaSourceError

# ── helpers ────────────────────────────────────────────────────────────────────


def _store() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


def _pub_id() -> str:
    return new_publication_id()


def _make_client(msg_id: int = 12345) -> AsyncMock:
    client = AsyncMock()
    sent = AsyncMock()
    sent.id = msg_id
    client.send_file.return_value = sent
    return client


def _video_post(tmp_path: Path, text: str = "Видео") -> CanonicalContent:
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"\x00\x00\x00\x18ftyp")  # minimal ftyp box
    return CanonicalContent(type=ContentType.POST, text=text, media_paths=[vid])


def _photo_post(tmp_path: Path, text: str = "Фото") -> CanonicalContent:
    img = tmp_path / "img.jpg"
    img.write_bytes(b"\xff\xd8\xff")
    return CanonicalContent(type=ContentType.POST, text=text, media_paths=[img])


# ── 1. Video post to Telegram → DocumentAttributeVideo with supports_streaming ─


@pytest.mark.asyncio
async def test_video_published_with_streaming_attribute(tmp_path):
    """send_file for a video must include DocumentAttributeVideo(supports_streaming=True)."""
    store = _store()
    pub_id = _pub_id()
    client = _make_client()
    content = _video_post(tmp_path)

    adapter = TelegramAdapter(client, target="@channel", store=store)
    result = await adapter.publish(content, publication_id=pub_id)

    assert result.status is ResultStatus.DONE
    client.send_file.assert_called_once()
    _, kwargs = client.send_file.call_args
    attrs = kwargs.get("attributes", [])
    from telethon.tl.types import DocumentAttributeVideo

    video_attrs = [a for a in attrs if isinstance(a, DocumentAttributeVideo)]
    assert video_attrs, "No DocumentAttributeVideo in send_file attributes"
    assert video_attrs[0].supports_streaming is True


@pytest.mark.asyncio
async def test_video_idempotent_skip(tmp_path):
    """Already-published video post is skipped without re-sending."""
    store = _store()
    pub_id = _pub_id()
    client = _make_client()
    store.mark_done(pub_id, "telegram", external_id="99")
    content = _video_post(tmp_path)

    adapter = TelegramAdapter(client, target="@channel", store=store)
    result = await adapter.publish(content, publication_id=pub_id)

    assert result.status is ResultStatus.SKIPPED
    client.send_file.assert_not_called()


# ── 2. Video to video-incapable channel → rejected at eligibility ──────────────


def test_telegram_supports_video():
    assert supports_video("telegram") is True


def test_telegraph_does_not_support_video():
    assert supports_video("telegraph") is False


def test_yandex_does_not_support_video():
    assert supports_video("yandex") is False


def test_vk_wall_does_not_support_video():
    assert supports_video("vk_wall") is False


# ── 3. Oversized video → explicit failure, nothing stored ─────────────────────


@pytest.mark.asyncio
async def test_stream_to_file_oversized_raises_nothing_stored(tmp_path, monkeypatch):
    """Video exceeding max_bytes → MediaSourceError; dest file not left on disk."""
    from crosspost.scheduler.media_sources import stream_to_file

    dest = tmp_path / "clip.mp4"
    chunk = b"V" * 1024  # 1 KB chunks

    async def _aiter():
        for _ in range(200):
            yield chunk

    resp = MagicMock()
    resp.status_code = 200
    resp.aiter_bytes = _aiter
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(
        "crosspost.scheduler.media_sources.assert_public_http_url",
        lambda *a, **kw: None,
    )

    class _FakeClient:
        def stream(self, *a, **kw):
            return resp

        async def aclose(self):
            pass

    with pytest.raises(MediaSourceError, match="размер|limit|превышает"):
        await stream_to_file(
            "https://example.com/clip.mp4",
            dest,
            client=_FakeClient(),
            max_bytes=100 * 1024,  # 100 KB cap
        )

    assert not dest.exists(), "Partial file must be cleaned up on error"


# ── 4. stream_to_file: streams to disk, honours SSRF guard ───────────────────


@pytest.mark.asyncio
async def test_stream_to_file_ssrf_guard_fires(tmp_path, monkeypatch):
    """SSRF-blocked URL → MediaSourceError before any download attempt."""
    from crosspost.scheduler.media_sources import stream_to_file

    monkeypatch.setattr(
        "crosspost.scheduler.media_sources._default_resolve",
        lambda _host: ["10.0.0.1"],
    )
    dest = tmp_path / "vid.mp4"
    with pytest.raises(MediaSourceError):
        await stream_to_file("http://internal.host/clip.mp4", dest)

    assert not dest.exists()


@pytest.mark.asyncio
async def test_stream_to_file_writes_to_disk(tmp_path, monkeypatch):
    """Successful download writes bytes to dest path, returns filename from URL."""
    from crosspost.scheduler.media_sources import stream_to_file

    content = b"FAKE_VIDEO_DATA" * 100

    async def _aiter():
        yield content

    resp = MagicMock()
    resp.status_code = 200
    resp.aiter_bytes = _aiter
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(
        "crosspost.scheduler.media_sources.assert_public_http_url",
        lambda *a, **kw: None,
    )

    class _FakeClient:
        def stream(self, *a, **kw):
            return resp

        async def aclose(self):
            pass

    dest = tmp_path / "out.mp4"
    fname = await stream_to_file(
        "https://cdn.example.com/clip.mp4",
        dest,
        client=_FakeClient(),
        max_bytes=10 * 1024 * 1024,
    )

    assert dest.exists()
    assert dest.read_bytes() == content
    assert fname == "clip.mp4"


# ── 5. Media kind detection is deterministic ──────────────────────────────────


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("clip.mp4", MediaKind.VIDEO),
        ("movie.mov", MediaKind.VIDEO),
        ("reel.webm", MediaKind.VIDEO),
        ("video.avi", MediaKind.VIDEO),
        ("photo.jpg", MediaKind.IMAGE),
        ("photo.jpeg", MediaKind.IMAGE),
        ("image.png", MediaKind.IMAGE),
        ("animation.gif", MediaKind.IMAGE),
        ("photo.JPEG", MediaKind.IMAGE),  # case-insensitive
        ("CLIP.MP4", MediaKind.VIDEO),  # case-insensitive
    ],
)
def test_detect_media_kind_by_extension(filename, expected, tmp_path):
    p = tmp_path / filename
    p.write_bytes(b"x")
    assert detect_media_kind(p) == expected


def test_detect_media_kind_from_string_path():
    assert detect_media_kind("/some/path/clip.mp4") == MediaKind.VIDEO
    assert detect_media_kind("/some/path/img.jpg") == MediaKind.IMAGE


# ── 6. Existing photo posting unchanged (regression) ──────────────────────────


@pytest.mark.asyncio
async def test_photo_post_regression(tmp_path):
    """Photo post still published via send_file without DocumentAttributeVideo."""
    store = _store()
    pub_id = _pub_id()
    client = _make_client()
    content = _photo_post(tmp_path, text="Caption")

    adapter = TelegramAdapter(client, target="@channel", store=store)
    result = await adapter.publish(content, publication_id=pub_id)

    assert result.status is ResultStatus.DONE
    client.send_file.assert_called_once()
    _, kwargs = client.send_file.call_args
    attrs = kwargs.get("attributes", [])
    # No DocumentAttributeVideo for plain image
    from telethon.tl.types import DocumentAttributeVideo

    assert not any(isinstance(a, DocumentAttributeVideo) for a in attrs)


@pytest.mark.asyncio
async def test_photo_post_send_file_caption_and_parse_mode(tmp_path):
    """Photo post: send_file called with correct caption and parse_mode (no regression)."""
    store = _store()
    pub_id = _pub_id()
    client = _make_client()
    content = _photo_post(tmp_path, text="<b>Жирный</b> текст")

    adapter = TelegramAdapter(client, target="@channel", store=store)
    result = await adapter.publish(content, publication_id=pub_id)

    assert result.status is ResultStatus.DONE
    _, kwargs = client.send_file.call_args
    assert kwargs["caption"] == "<b>Жирный</b> текст"
    assert kwargs["parse_mode"] == "html"
