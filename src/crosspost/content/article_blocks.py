"""Article content model: ordered blocks (paragraphs + images).

parse_blocks: flat text → list[Block], deterministic, no guessing.
resolve_images: downloads ImageBlock URLs via fetch_media (SSRF guard included),
    stores bytes in the media store (save_binaries), sets block.path.
    Failed blocks are reported per-block; no silent drops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from crosspost.scheduler.media_sources import MediaSourceError, fetch_media
from crosspost.web.media import save_binaries


@dataclass
class ParagraphBlock:
    type: Literal["p"] = field(default="p", init=False, repr=False)
    text: str


@dataclass
class ImageBlock:
    type: Literal["img"] = field(default="img", init=False, repr=False)
    url: str
    path: str = ""  # local path after resolve_images; empty until resolved


Block = ParagraphBlock | ImageBlock


def parse_blocks(text: str) -> list[Block]:
    """Parse flat text into ordered blocks, one block per non-empty line.

    Each line is trimmed; blank/whitespace-only lines are dropped.
    A line whose entire content is a single URL (http/https, no whitespace)
    → ImageBlock. Every other non-empty line → ParagraphBlock.
    Windows \r\n and Unix \n are handled identically.
    """
    blocks: list[Block] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _is_sole_url(stripped):
            blocks.append(ImageBlock(url=stripped))
        else:
            blocks.append(ParagraphBlock(text=stripped))
    return blocks


def _is_sole_url(s: str) -> bool:
    return s.startswith(("http://", "https://")) and not any(c.isspace() for c in s)


@dataclass
class ImageResolutionError:
    block_index: int
    url: str
    reason: str


@dataclass
class ResolveResult:
    blocks: list[Block]
    errors: list[ImageResolutionError]


async def resolve_images(
    blocks: list[Block],
    key: str,
    *,
    folder: str = "",
    client=None,
    resolve=None,
) -> ResolveResult:
    """Download image URLs via fetch_media, store in media_store(key).

    Sets ImageBlock.path for successful fetches. Reports failures per-block
    without dropping the block, consistent with the import binding report.
    """
    errors: list[ImageResolutionError] = []
    fetched: list[tuple[int, str, bytes]] = []  # (block_index, filename, data)

    for i, block in enumerate(blocks):
        if not isinstance(block, ImageBlock):
            continue
        try:
            media = await fetch_media(block.url, folder=folder, client=client, resolve=resolve)
            fetched.append((i, media.filename, media.data))
        except MediaSourceError as exc:
            errors.append(ImageResolutionError(block_index=i, url=block.url, reason=str(exc)))

    if fetched:
        paths = save_binaries([(name, data) for _, name, data in fetched], key)
        for (block_idx, _, _), path in zip(fetched, paths, strict=True):
            blocks[block_idx].path = path  # type: ignore[union-attr]

    return ResolveResult(blocks=blocks, errors=errors)
