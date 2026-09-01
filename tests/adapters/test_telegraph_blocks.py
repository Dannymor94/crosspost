"""Tests for block-based Telegraph rendering (increment 2).

Telegraph Node spec: https://telegra.ph/api#NodeElement
  image node: {"tag": "img", "attrs": {"src": "https://telegra.ph/file/..."}}
  Upload endpoint: POST https://telegra.ph/upload → [{"src": "/file/..."}]
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from crosspost.adapters.api.telegraph import TelegraphAdapter
from crosspost.adapters.base import ResultStatus
from crosspost.content.article_blocks import ImageBlock, ParagraphBlock
from crosspost.content.canonical import CanonicalContent, ContentType

_TOKEN = "test-token"
_PAGE_URL = "https://telegra.ph/Test-01-01"
_UPLOAD_SRC = "/file/abc123img"
_PATCH_TARGET = "crosspost.adapters.api.telegraph.httpx.AsyncClient"


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_client(
    create_resp: dict,
    upload_resp: list | None = None,
) -> MagicMock:
    """Mock httpx.AsyncClient: first post() = upload (if upload_resp), last = createPage."""
    upload_resp = upload_resp or []

    upload_r = MagicMock()
    upload_r.raise_for_status = MagicMock()
    upload_r.json.return_value = upload_resp

    create_r = MagicMock()
    create_r.raise_for_status = MagicMock()
    create_r.json.return_value = create_resp

    client = AsyncMock()
    # side_effect: each call pops from a queue
    _responses = [upload_r] * 10 + [create_r]

    async def _post(url, **kwargs):
        if "upload" in url:
            return upload_r
        return create_r

    client.post = AsyncMock(side_effect=_post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _ok_create() -> dict:
    return {"ok": True, "result": {"url": _PAGE_URL, "path": "Test-01-01"}}


# ── 1. Blocks with images render as ordered nodes ─────────────────────────────


async def test_blocks_render_in_order(store, publication_id, tmp_path):
    """paragraph / image / paragraph renders as p / img / p in that order."""
    img_file = tmp_path / "photo.jpg"
    img_file.write_bytes(b"\xff\xd8\xff")

    content = CanonicalContent(
        type=ContentType.ARTICLE,
        title="Тест",
        blocks=[
            ParagraphBlock(text="Вступление."),
            ImageBlock(url="https://example.com/x", path=str(img_file)),
            ParagraphBlock(text="Заключение."),
        ],
    )

    client = _make_client(
        _ok_create(),
        upload_resp=[{"src": _UPLOAD_SRC}],
    )
    with patch(_PATCH_TARGET, return_value=client):
        adapter = TelegraphAdapter(_TOKEN, store)
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE

    create_call = next(c for c in client.post.call_args_list if "createPage" in c.args[0])
    nodes = json.loads(create_call.kwargs["data"]["content"])

    assert len(nodes) == 3
    assert nodes[0]["tag"] == "p"
    assert nodes[1]["tag"] == "img"
    assert nodes[2]["tag"] == "p"
    # image src must be the telegraph URL
    assert "telegra.ph" in nodes[1]["attrs"]["src"]
    assert _UPLOAD_SRC.lstrip("/") in nodes[1]["attrs"]["src"]


# ── 2. Title from title field; absent title with blocks → FAILED ───────────────


async def test_title_from_blocks_content(store, publication_id, tmp_path):
    content = CanonicalContent(
        type=ContentType.ARTICLE,
        title="Явный заголовок",
        blocks=[ParagraphBlock(text="Тело.")],
    )
    client = _make_client(_ok_create())
    with patch(_PATCH_TARGET, return_value=client):
        adapter = TelegraphAdapter(_TOKEN, store)
        await adapter.publish(content, publication_id=publication_id)

    create_call = next(c for c in client.post.call_args_list if "createPage" in c.args[0])
    assert create_call.kwargs["data"]["title"] == "Явный заголовок"


async def test_absent_title_with_blocks_is_failed(store, publication_id):
    """blocks + no title → FAILED (not mid-sentence truncation)."""
    content = CanonicalContent(
        type=ContentType.ARTICLE,
        title=None,
        blocks=[
            ParagraphBlock(text="Очень длинный параграф который не должен становиться заголовком.")
        ],
    )
    client = _make_client(_ok_create())
    with patch(_PATCH_TARGET, return_value=client):
        adapter = TelegraphAdapter(_TOKEN, store)
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.FAILED
    assert "title" in (result.error or "").lower()
    client.post.assert_not_called()


# ── 3. article_flow contract: Telegraph failure → TG post not sent ─────────────


async def test_fail_closed_on_telegraph_failure(store, publication_id):
    """If Telegraph returns ok=False, article_flow must not publish to TG channels.

    Covered by the existing test_article_flow tests; here we verify the adapter
    returns FAILED so the sequencer can act on it.
    """
    content = CanonicalContent(
        type=ContentType.ARTICLE,
        title="Заголовок",
        blocks=[ParagraphBlock(text="Тело.")],
    )
    client = _make_client({"ok": False, "error": "SOME_ERROR"})
    with patch(_PATCH_TARGET, return_value=client):
        adapter = TelegraphAdapter(_TOKEN, store)
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.FAILED
    assert not store.is_done(publication_id, "telegraph")


# ── 4. Legacy flat-text article still publishes correctly ─────────────────────


async def test_legacy_flat_text_publishes(store, publication_id):
    """CanonicalContent with text but no blocks → legacy path, still works."""
    content = CanonicalContent(
        type=ContentType.ARTICLE,
        title="Заголовок",
        text="Первый параграф.\n\nВторой параграф.",
    )
    client = _make_client(_ok_create())
    with patch(_PATCH_TARGET, return_value=client):
        adapter = TelegraphAdapter(_TOKEN, store)
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.DONE

    create_call = next(c for c in client.post.call_args_list if "createPage" in c.args[0])
    nodes = json.loads(create_call.kwargs["data"]["content"])
    assert len(nodes) == 2
    assert all(n["tag"] == "p" for n in nodes)


async def test_legacy_fallback_title(store, publication_id):
    """Legacy path: no title → falls back to text[:256] (existing behaviour preserved)."""
    content = CanonicalContent(
        type=ContentType.ARTICLE,
        title=None,
        text="Текст без явного заголовка.",
    )
    client = _make_client(_ok_create())
    with patch(_PATCH_TARGET, return_value=client):
        adapter = TelegraphAdapter(_TOKEN, store)
        await adapter.publish(content, publication_id=publication_id)

    create_call = next(c for c in client.post.call_args_list if "createPage" in c.args[0])
    assert create_call.kwargs["data"]["title"] == "Текст без явного заголовка."


# ── 5. Image block with missing/empty path → explicit FAILED ──────────────────


async def test_image_block_missing_file_is_failed(store, publication_id):
    """ImageBlock.path == '' (not yet resolved) → FAILED, not a silently skipped image."""
    content = CanonicalContent(
        type=ContentType.ARTICLE,
        title="Заголовок",
        blocks=[
            ParagraphBlock(text="Текст."),
            ImageBlock(url="https://example.com/x", path=""),  # not resolved
        ],
    )
    client = _make_client(_ok_create())
    with patch(_PATCH_TARGET, return_value=client):
        adapter = TelegraphAdapter(_TOKEN, store)
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.FAILED
    assert result.error
    client.post.assert_not_called()


async def test_image_block_nonexistent_file_is_failed(store, publication_id, tmp_path):
    """ImageBlock.path points to a file that doesn't exist → FAILED."""
    content = CanonicalContent(
        type=ContentType.ARTICLE,
        title="Заголовок",
        blocks=[
            ImageBlock(url="https://example.com/x", path=str(tmp_path / "gone.jpg")),
        ],
    )
    client = _make_client(_ok_create())
    with patch(_PATCH_TARGET, return_value=client):
        adapter = TelegraphAdapter(_TOKEN, store)
        result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.FAILED
    client.post.assert_not_called()
