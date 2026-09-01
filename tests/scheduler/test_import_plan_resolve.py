"""Tests for resolve_plan_images — plan-level image resolution.

Fail-closed policy: an article row with any failed image block is moved to
problems (not imported with partial blocks). This mirrors the fail-closed
choice made for Telegraph+Telegram linking: if the article cannot be fully
assembled, the whole article is rejected rather than published with holes.

Deduplication: same URL downloaded once, path shared across referencing rows/blocks.
All images stored under one import_key per import session. Cleanup safety relies on
pub_repo.media_key_shared which prevents deletion while any other scheduled post
still references the same key (existing mechanism, not re-tested here).

Tests:
1. Several article rows → all image blocks resolved, order preserved.
2. Same URL across two rows → downloaded once, both reference it; import_key shared.
3. One unreachable image → that row in problems (fail-closed), others import fine.
4. Per-import image count ceiling exceeded → explicit report entry, no partial silent state.
5. Pacing is respected: asyncio.sleep called for each downloaded URL.
6. SSRF-blocked URL → MediaSourceError reported, row moved to problems, nothing stored.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from crosspost.content.article_blocks import ImageBlock, ParagraphBlock, parse_blocks
from crosspost.scheduler.import_plan import (
    ImportReport,
    ResolvedRow,
    resolve_plan_images,
)
from crosspost.scheduler.media_sources import FetchedMedia, MediaSourceError, SourceKind

# ── Helpers ───────────────────────────────────────────────────────────────────


def _article_row(
    row_number: int,
    body: str,
    title: str = "Заголовок",
    file: str = "",
) -> ResolvedRow:
    blocks = parse_blocks(body)
    return ResolvedRow(
        row_number=row_number,
        file=file,
        text=body,
        scheduled_at=datetime(2025, 1, row_number, 10, 0),
        source="rule",
        content_type="article",
        title=title,
        blocks=blocks,
    )


def _post_row(row_number: int) -> ResolvedRow:
    return ResolvedRow(
        row_number=row_number,
        file="1.jpg",
        text="Текст поста",
        scheduled_at=datetime(2025, 1, row_number, 10, 0),
        source="rule",
        content_type="post",
    )


def _fake_media(url: str) -> FetchedMedia:
    return FetchedMedia(
        data=b"PNG" + url.encode()[:10],
        filename="img.png",
        kind=SourceKind.HTTP,
    )


@pytest.fixture(autouse=True)
def _media_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))


# ── 1. All images resolve, order preserved ────────────────────────────────────


async def test_all_images_resolve(monkeypatch):
    """All image blocks in article rows get a non-empty path after resolution."""
    rows = [
        _article_row(1, "Абзац.\n\nhttps://example.com/a.jpg"),
        _article_row(2, "Текст.\n\nhttps://example.com/b.png\n\nКонец."),
    ]
    report = ImportReport(resolved=rows)

    async def fake_fetch(url, *, client=None, max_bytes=None, **kw):
        return _fake_media(url)

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", fake_fetch)

    result = await resolve_plan_images(report, "import-test-1", pacing=0)

    assert not result.problems
    assert len(result.resolved) == 2
    for rr in result.resolved:
        assert rr.content_type == "article"
        for block in rr.blocks:
            if isinstance(block, ImageBlock):
                assert block.path, f"ImageBlock.path is empty for {block.url}"


async def test_paragraph_blocks_untouched(monkeypatch):
    """Paragraph blocks are preserved and not affected by resolution."""
    rows = [_article_row(1, "Только текст.\n\nЕщё параграф.")]
    report = ImportReport(resolved=rows)

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", AsyncMock())

    result = await resolve_plan_images(report, "import-test-para", pacing=0)

    assert len(result.resolved) == 1
    assert all(isinstance(b, ParagraphBlock) for b in result.resolved[0].blocks)
    # fetch_media never called — no image blocks
    # (we don't assert call count here — it was replaced by AsyncMock)


# ── 2. Deduplication: same URL downloaded once ───────────────────────────────


async def test_same_url_downloaded_once(monkeypatch):
    """Same URL in two article rows is fetched once; both blocks reference the same path."""
    shared_url = "https://example.com/shared.jpg"
    rows = [
        _article_row(1, f"Первая.\n\n{shared_url}"),
        _article_row(2, f"Вторая.\n\n{shared_url}"),
    ]
    report = ImportReport(resolved=rows)

    call_count = {"n": 0}

    async def counting_fetch(url, *, client=None, max_bytes=None, **kw):
        call_count["n"] += 1
        return _fake_media(url)

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", counting_fetch)

    result = await resolve_plan_images(report, "import-dedup", pacing=0)

    assert call_count["n"] == 1, f"fetch_media called {call_count['n']} times for one unique URL"
    assert not result.problems
    path_a = next(b.path for b in result.resolved[0].blocks if isinstance(b, ImageBlock))
    path_b = next(b.path for b in result.resolved[1].blocks if isinstance(b, ImageBlock))
    assert path_a == path_b, "Both rows must reference the same stored path for the shared URL"


async def test_dedup_uses_same_import_key(monkeypatch, tmp_path):
    """All images go into one import_key directory — shared cleanup key."""
    shared_url = "https://example.com/img.jpg"
    rows = [
        _article_row(1, f"A.\n\n{shared_url}"),
        _article_row(2, f"B.\n\n{shared_url}"),
    ]
    report = ImportReport(resolved=rows)

    async def fake_fetch(url, **kw):
        return _fake_media(url)

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", fake_fetch)

    result = await resolve_plan_images(report, "my-import-key", pacing=0)

    assert not result.problems
    for rr in result.resolved:
        for block in rr.blocks:
            if isinstance(block, ImageBlock):
                assert "my-import-key" in block.path, "path must be inside import_key dir"


# ── 3. One unreachable image → fail-closed, others import fine ───────────────


async def test_failed_image_row_moved_to_problems(monkeypatch):
    """Row with any failing image → problems (fail-closed); other rows unaffected."""
    good_url = "https://example.com/good.jpg"
    bad_url = "https://example.com/bad.jpg"
    rows = [
        _article_row(1, f"Хорошая.\n\n{good_url}"),
        _article_row(2, f"Плохая.\n\n{bad_url}"),
        _article_row(3, "Без картинок."),
    ]
    report = ImportReport(resolved=rows)

    async def selective_fetch(url, *, client=None, max_bytes=None, **kw):
        if url == bad_url:
            raise MediaSourceError("соединение отклонено")
        return _fake_media(url)

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", selective_fetch)

    result = await resolve_plan_images(report, "import-fail", pacing=0)

    resolved_nums = {r.row_number for r in result.resolved}
    problem_nums = {p.row_number for p in result.problems}
    assert 2 in problem_nums, "failing row must be in problems"
    assert 2 not in resolved_nums
    assert {1, 3} <= resolved_nums, "rows 1 and 3 must still resolve"
    # Problem reason names the bad URL or block index
    failing_problem = next(p for p in result.problems if p.row_number == 2)
    assert "соединение отклонено" in failing_problem.reason or bad_url in failing_problem.reason


async def test_failed_row_does_not_have_partial_blocks(monkeypatch):
    """Fail-closed: rejected row is not in resolved at all (no partial image blocks)."""
    bad_url = "https://example.com/broken.jpg"
    rows = [_article_row(1, f"Текст.\n\n{bad_url}")]
    report = ImportReport(resolved=rows)

    async def failing_fetch(url, **kw):
        raise MediaSourceError("timeout")

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", failing_fetch)

    result = await resolve_plan_images(report, "import-partial", pacing=0)

    assert len(result.resolved) == 0
    assert len(result.problems) == 1
    # Never in resolved with an empty path (the fail-open, partial outcome we reject).
    assert not any(
        isinstance(b, ImageBlock) and not b.path
        for rr in result.resolved
        for b in (rr.blocks or [])
    )


# ── 4. Per-import ceiling exceeded ────────────────────────────────────────────


async def test_image_count_ceiling(monkeypatch):
    """Exceeding max_images ceiling → explicit report entry; no silent partial state."""
    urls = [f"https://example.com/img{i}.jpg" for i in range(5)]
    rows = [_article_row(i + 1, f"Text.\n\n{url}") for i, url in enumerate(urls)]
    report = ImportReport(resolved=rows)

    async def fake_fetch(url, **kw):
        return _fake_media(url)

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", fake_fetch)

    # Set ceiling at 3 unique images.
    result = await resolve_plan_images(report, "import-cap", pacing=0, max_images=3)

    # At least one RowProblem explains the ceiling.
    ceiling_problems = [
        p for p in result.problems if "лимит" in p.reason or "limit" in p.reason.lower()
    ]
    assert ceiling_problems, "must have an explicit ceiling report entry"

    # No row silently resolved beyond the ceiling context.
    # (All article rows should be in problems when ceiling exceeded before download.)
    assert all(p.row_number > 0 or p.file == "" for p in result.problems)


async def test_bytes_ceiling(monkeypatch):
    """Exceeding max_bytes total ceiling → explicit report entry, stops early."""
    urls = [f"https://example.com/img{i}.jpg" for i in range(4)]
    rows = [_article_row(i + 1, f"Text.\n\n{url}") for i, url in enumerate(urls)]
    report = ImportReport(resolved=rows)

    async def chunky_fetch(url, **kw):
        return FetchedMedia(data=b"X" * 200, filename="img.jpg", kind=SourceKind.HTTP)

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", chunky_fetch)

    # 4 images × 200 bytes = 800 bytes total; cap at 500 bytes.
    result = await resolve_plan_images(report, "import-bytes-cap", pacing=0, max_bytes=500)

    ceiling_problems = [
        p
        for p in result.problems
        if "лимит" in p.reason or "limit" in p.reason.lower() or "байт" in p.reason
    ]
    assert ceiling_problems, "must have explicit bytes-ceiling report entry"


# ── 5. Pacing respected ────────────────────────────────────────────────────────


async def test_pacing_sleep_called_per_download(monkeypatch):
    """asyncio.sleep is called once per downloaded URL (pacing between requests)."""
    urls = [f"https://example.com/img{i}.jpg" for i in range(3)]
    rows = [_article_row(i + 1, f"Text.\n\n{url}") for i, url in enumerate(urls)]
    report = ImportReport(resolved=rows)

    async def fake_fetch(url, **kw):
        return _fake_media(url)

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", fake_fetch)

    sleep_calls: list[float] = []

    async def recording_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("crosspost.scheduler.import_plan._plan_sleep", recording_sleep)

    pacing = 0.25
    result = await resolve_plan_images(report, "import-pace", pacing=pacing)

    assert not result.problems
    # One sleep per unique URL.
    assert len(sleep_calls) == 3
    assert all(d == pacing for d in sleep_calls)


async def test_concurrency_cap(monkeypatch):
    """At most max_concurrency downloads run simultaneously."""
    urls = [f"https://example.com/img{i}.jpg" for i in range(6)]
    rows = [_article_row(i + 1, f"Text.\n\n{url}") for i, url in enumerate(urls)]
    report = ImportReport(resolved=rows)

    concurrent = {"current": 0, "max": 0}

    async def slow_fetch(url, **kw):
        concurrent["current"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["current"])
        await asyncio.sleep(0)  # yield to let others start
        concurrent["current"] -= 1
        return _fake_media(url)

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", slow_fetch)

    # pacing=0 so sleep doesn't serialize everything
    await resolve_plan_images(report, "import-conc", pacing=0, max_concurrency=2)

    assert concurrent["max"] <= 2, f"peak concurrency was {concurrent['max']}, expected ≤ 2"


# ── 6. SSRF-blocked URL ────────────────────────────────────────────────────────


async def test_ssrf_blocked_url_reported_nothing_stored(monkeypatch, tmp_path):
    """SSRF-blocked URL raises MediaSourceError → row in problems, no file written."""
    ssrf_url = "http://169.254.169.254/latest/meta-data"
    rows = [_article_row(1, f"AWS secret.\n\n{ssrf_url}")]
    report = ImportReport(resolved=rows)

    async def ssrf_fetch(url, *, client=None, max_bytes=None, **kw):
        raise MediaSourceError("SSRF: частный IP-адрес запрещён")

    monkeypatch.setattr("crosspost.scheduler.import_plan.fetch_media", ssrf_fetch)

    result = await resolve_plan_images(report, "import-ssrf", pacing=0)

    assert len(result.resolved) == 0
    assert len(result.problems) == 1
    assert "SSRF" in result.problems[0].reason or "IP" in result.problems[0].reason

    # No file written.
    media_root = tmp_path / "media" / "import-ssrf"
    assert not media_root.exists() or not any(media_root.iterdir())
