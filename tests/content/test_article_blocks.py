"""Tests for article_blocks: parse_blocks + resolve_images."""

from __future__ import annotations

from pathlib import Path

import httpx

from crosspost.content.article_blocks import (
    ImageBlock,
    ParagraphBlock,
    parse_blocks,
    resolve_images,
)

# ── parse_blocks ──────────────────────────────────────────────────────────────


def test_alternating_blocks_in_order():
    text = "Hello world\n\nhttps://example.com/img.jpg\n\nFoo bar\n\nhttps://example.com/img2.jpg"
    blocks = parse_blocks(text)
    assert len(blocks) == 4
    assert isinstance(blocks[0], ParagraphBlock)
    assert isinstance(blocks[1], ImageBlock)
    assert isinstance(blocks[2], ParagraphBlock)
    assert isinstance(blocks[3], ImageBlock)
    assert blocks[0].text == "Hello world"
    assert blocks[1].url == "https://example.com/img.jpg"
    assert blocks[2].text == "Foo bar"
    assert blocks[3].url == "https://example.com/img2.jpg"


def test_inline_url_stays_paragraph():
    text = "Check https://example.com/img.jpg for details"
    blocks = parse_blocks(text)
    assert len(blocks) == 1
    assert isinstance(blocks[0], ParagraphBlock)
    assert "https://example.com/img.jpg" in blocks[0].text


def test_extensionless_url_on_own_line_becomes_image():
    url = "https://lh3.googleusercontent.com/proxy/abc?w=200&h=200"
    blocks = parse_blocks(url)
    assert len(blocks) == 1
    assert isinstance(blocks[0], ImageBlock)
    assert blocks[0].url == url


def test_blank_lines_and_empty_paragraphs_dropped():
    text = "\n\nHello\n\n\n\nWorld\n\n"
    blocks = parse_blocks(text)
    assert len(blocks) == 2
    assert isinstance(blocks[0], ParagraphBlock)
    assert isinstance(blocks[1], ParagraphBlock)


def test_empty_text_returns_no_blocks():
    assert parse_blocks("") == []
    assert parse_blocks("   \n\n   ") == []


# ── resolve_images ─────────────────────────────────────────────────────────────


async def test_ssrf_blocked_host_reported_per_block():
    """Private-IP URL → error reported for that block; block path stays empty."""
    blocks = [ImageBlock(url="http://169.254.169.254/metadata")]

    def resolve_private(_host: str) -> list[str]:
        return ["169.254.169.254"]

    result = await resolve_images(blocks, key="test-ssrf", resolve=resolve_private)
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err.block_index == 0
    assert err.url == "http://169.254.169.254/metadata"
    assert "приватный" in err.reason.lower() or "private" in err.reason.lower()
    assert blocks[0].path == ""


async def test_path_traversal_url_rejected():
    """URL with SSRF-failing host is rejected and reported per-block."""
    blocks = [ImageBlock(url="http://192.168.1.1/secret")]

    def resolve_private(_host: str) -> list[str]:
        return ["192.168.1.1"]

    result = await resolve_images(blocks, key="test-traversal", resolve=resolve_private)
    assert len(result.errors) == 1
    assert blocks[0].path == ""


async def test_resolved_image_lands_in_media_store(tmp_path, monkeypatch):
    """Successful fetch → bytes written to media store, block.path points to real file."""
    import crosspost.web.media as media_mod

    monkeypatch.setattr(media_mod, "_media_root", lambda: tmp_path)

    image_bytes = b"\x89PNG\r\nFAKEDATA"
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=image_bytes))
    client = httpx.AsyncClient(transport=transport)

    def resolve_public(_host: str) -> list[str]:
        return ["93.184.216.34"]  # globally routable

    blocks = parse_blocks("https://example.com/photo")
    assert len(blocks) == 1
    assert isinstance(blocks[0], ImageBlock)

    result = await resolve_images(blocks, key="art-001", client=client, resolve=resolve_public)
    assert result.errors == []
    assert blocks[0].path != ""
    p = Path(blocks[0].path)
    assert p.exists()
    assert p.read_bytes() == image_bytes
    # must live inside the media store for this key
    assert "art-001" in str(p)


async def test_failed_resolution_does_not_affect_successful_blocks(tmp_path, monkeypatch):
    """One bad block → error; other image blocks still resolved."""
    import crosspost.web.media as media_mod

    monkeypatch.setattr(media_mod, "_media_root", lambda: tmp_path)

    image_bytes = b"OK"

    def transport_handler(req: httpx.Request) -> httpx.Response:
        if "good" in str(req.url):
            return httpx.Response(200, content=image_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(transport_handler)
    client = httpx.AsyncClient(transport=transport)

    def resolve_public(_host: str) -> list[str]:
        return ["93.184.216.34"]

    blocks = [
        ImageBlock(url="https://example.com/good"),
        ImageBlock(url="https://example.com/bad"),
    ]
    result = await resolve_images(blocks, key="art-002", client=client, resolve=resolve_public)
    assert len(result.errors) == 1
    assert result.errors[0].block_index == 1
    assert blocks[0].path != ""
    assert blocks[1].path == ""
