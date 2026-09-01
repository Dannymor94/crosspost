"""Telegraph-адаптер. API-тир.

Публикует статью (ContentType.ARTICLE) через HTTP REST API Telegraph.
post / reel / story → SKIPPED: не поддерживаются платформой.
Идемпотентность по внутреннему publication_id.

API: https://telegra.ph/api
  Используемые методы:
    createPage  — POST https://api.telegra.ph/createPage
  Загрузка медиа:
    POST https://telegra.ph/upload (multipart) → [{"src": "/file/..."}]

Два пути контента:
  blocks path  — content.blocks задан (article_blocks.Block[]); title обязателен.
  legacy path  — content.blocks is None; рендер из content.text; title с fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.orchestrator.task import IdempotencyStore

_API_BASE = "https://api.telegra.ph"
_UPLOAD_URL = "https://telegra.ph/upload"

# Legacy fallback when title is absent and blocks are not used.
_LEGACY_TITLE_FALLBACK = "Untitled"

# Error text returned when blocks path has no title.
_MISSING_TITLE_ERROR = "article title is required when publishing from blocks"


def _text_to_nodes(text: str) -> list[dict]:
    """Текст статьи → массив Telegraph Node.

    Параграфы разделяются двойным переносом строки (\\n\\n).
    Одиночный перенос внутри параграфа → тег <br>.

    Spec: https://telegra.ph/api#NodeElement
    Поддерживаемые теги: p, br, b, i, a, code, pre, h3, h4, …
    """
    nodes: list[dict] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        children: list = []
        for i, line in enumerate(block.split("\n")):
            if i > 0:
                children.append({"tag": "br"})
            if line:
                children.append(line)
        nodes.append({"tag": "p", "children": children or [""]})
    return nodes or [{"tag": "p", "children": [text or ""]}]


async def _upload_image(client: httpx.AsyncClient, path: str) -> str:
    """Upload a local image file to Telegraph. Returns the full https://telegra.ph/file/... URL.

    Telegraph upload: POST https://telegra.ph/upload, multipart field 'file'.
    Response: [{"src": "/file/<hash>"}]
    """
    data = Path(path).read_bytes()
    filename = Path(path).name
    resp = await client.post(
        _UPLOAD_URL,
        files={"file": (filename, data)},
    )
    resp.raise_for_status()
    payload = resp.json()
    src = payload[0]["src"]  # e.g. "/file/abc123"
    return f"https://telegra.ph{src}"


async def _blocks_to_nodes(blocks: list, client: httpx.AsyncClient) -> list[dict]:
    """Convert article_blocks.Block list to Telegraph Node list.

    Uploads image files to Telegraph and returns img nodes with telegra.ph src URLs.
    Raises ValueError for ImageBlock with empty or missing path.
    """
    from crosspost.content.article_blocks import ImageBlock, ParagraphBlock

    nodes: list[dict] = []
    for block in blocks:
        if isinstance(block, ParagraphBlock):
            children: list = []
            for i, line in enumerate(block.text.split("\n")):
                if i > 0:
                    children.append({"tag": "br"})
                if line:
                    children.append(line)
            nodes.append({"tag": "p", "children": children or [""]})
        elif isinstance(block, ImageBlock):
            if not block.path:
                raise ValueError(f"ImageBlock has no resolved path (url={block.url!r})")
            if not Path(block.path).exists():
                raise ValueError(f"ImageBlock file not found: {block.path!r}")
            src_url = await _upload_image(client, block.path)
            nodes.append({"tag": "img", "attrs": {"src": src_url}})
    return nodes or [{"tag": "p", "children": [""]}]


class TelegraphAdapter:
    channel = "telegraph"

    def __init__(self, access_token: str, store: IdempotencyStore) -> None:
        self._token = access_token
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

        # 2) Telegraph поддерживает только статьи
        if content.type != ContentType.ARTICLE:
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        # 3) title validation for blocks path
        if content.blocks is not None and content.title is None:
            return ChannelResult(self.channel, ResultStatus.FAILED, error=_MISSING_TITLE_ERROR)

        # 4) pre-flight validation of image blocks (no I/O, fast check)
        if content.blocks is not None:
            from crosspost.content.article_blocks import ImageBlock

            for block in content.blocks:
                if isinstance(block, ImageBlock):
                    if not block.path:
                        return ChannelResult(
                            self.channel,
                            ResultStatus.FAILED,
                            error=f"ImageBlock has no resolved path (url={block.url!r})",
                        )
                    if not Path(block.path).exists():
                        return ChannelResult(
                            self.channel,
                            ResultStatus.FAILED,
                            error=f"ImageBlock file not found: {block.path!r}",
                        )

        # 5) render + POST createPage
        async with httpx.AsyncClient(timeout=30) as client:
            if content.blocks is not None:
                try:
                    nodes = await _blocks_to_nodes(content.blocks, client)
                except ValueError as exc:
                    return ChannelResult(self.channel, ResultStatus.FAILED, error=str(exc))
                # title guaranteed non-None here (checked above)
                title = content.title.strip()[:256]  # type: ignore[union-attr]
            else:
                # legacy flat-text path
                nodes = _text_to_nodes(content.text)
                title = (content.title or content.text[:256] or _LEGACY_TITLE_FALLBACK).strip()[
                    :256
                ]

            resp = await client.post(
                f"{_API_BASE}/createPage",
                data={
                    "access_token": self._token,
                    "title": title,
                    "content": json.dumps(nodes, ensure_ascii=False),
                },
            )
        resp.raise_for_status()
        payload = resp.json()

        if not payload.get("ok"):
            error = payload.get("error", "unknown Telegraph error")
            return ChannelResult(self.channel, ResultStatus.FAILED, error=error)

        # 6) квитанция — полный URL страницы (для верификации / правки / удаления)
        external_id = payload["result"]["url"]
        self._store.mark_done(publication_id, self.channel, external_id=external_id)
        return ChannelResult(self.channel, ResultStatus.DONE, external_id=external_id)


# проверка соответствия контракту на этапе импорта/тайпчека
_: type[ChannelAdapter] = TelegraphAdapter  # noqa: E305
