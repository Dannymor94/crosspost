"""RED→GREEN: parse_blocks must split on single newlines, not only blank lines.

Single-newline-separated lines should each become their own block:
  - A line with only an http(s) URL → ImageBlock
  - Any other non-empty line → ParagraphBlock
  - Blank lines (empty / whitespace-only) → dropped, never become blocks
"""

from crosspost.content.article_blocks import ImageBlock, ParagraphBlock, parse_blocks

# ── 1. Single newline produces p / img / p ────────────────────────────────────


def test_single_newline_between_text_url_text():
    """The canonical failing case: single newlines must split into separate blocks."""
    text = "Текст\nhttps://example.com/pic.jpg\nтекст"
    blocks = parse_blocks(text)
    assert len(blocks) == 3, f"expected 3 blocks, got {len(blocks)}: {blocks}"
    assert isinstance(blocks[0], ParagraphBlock)
    assert isinstance(blocks[1], ImageBlock)
    assert isinstance(blocks[2], ParagraphBlock)
    assert blocks[0].text == "Текст"
    assert blocks[1].url == "https://example.com/pic.jpg"
    assert blocks[2].text == "текст"


def test_single_newline_url_first():
    """URL as first line, text below — single newline."""
    blocks = parse_blocks("https://example.com/img.jpg\nПодпись к фото")
    assert len(blocks) == 2
    assert isinstance(blocks[0], ImageBlock)
    assert isinstance(blocks[1], ParagraphBlock)


def test_single_newline_multiple_text_lines():
    """Three text lines with single newlines → three paragraph blocks."""
    blocks = parse_blocks("Первый\nВторой\nТретий")
    assert len(blocks) == 3
    assert all(isinstance(b, ParagraphBlock) for b in blocks)
    assert [b.text for b in blocks] == ["Первый", "Второй", "Третий"]


# ── 2. Blank lines produce no extra blocks ────────────────────────────────────


def test_blank_lines_between_content_lines_dropped():
    """Blank lines between lines produce no extra blocks and no empty paragraphs."""
    text = "Первый\n\nВторой\n\n\nТретий"
    blocks = parse_blocks(text)
    assert len(blocks) == 3
    assert all(isinstance(b, ParagraphBlock) for b in blocks)
    assert [b.text for b in blocks] == ["Первый", "Второй", "Третий"]


def test_blank_lines_around_url():
    """Blank lines around URL line: just the URL block, no empty paragraph."""
    text = "\n\nhttps://example.com/img.jpg\n\n"
    blocks = parse_blocks(text)
    assert len(blocks) == 1
    assert isinstance(blocks[0], ImageBlock)


def test_whitespace_only_lines_dropped():
    """Lines with only spaces/tabs are treated as blank (dropped)."""
    text = "Первый\n   \nhttps://example.com/img.jpg\n\t\nВторой"
    blocks = parse_blocks(text)
    assert len(blocks) == 3
    assert isinstance(blocks[0], ParagraphBlock)
    assert isinstance(blocks[1], ImageBlock)
    assert isinstance(blocks[2], ParagraphBlock)


# ── 3. Inline URL stays paragraph ─────────────────────────────────────────────


def test_inline_url_among_words_stays_paragraph():
    """URL not alone on its line stays inside the paragraph block."""
    text = "Смотрите https://example.com/img.jpg для деталей"
    blocks = parse_blocks(text)
    assert len(blocks) == 1
    assert isinstance(blocks[0], ParagraphBlock)
    assert "https://example.com/img.jpg" in blocks[0].text


def test_url_with_trailing_text_stays_paragraph():
    """URL followed by text on same line → paragraph."""
    text = "https://example.com/img.jpg — отличное фото"
    blocks = parse_blocks(text)
    assert len(blocks) == 1
    assert isinstance(blocks[0], ParagraphBlock)


# ── 4. Plan import consistency with preview endpoint ─────────────────────────


def test_plan_import_single_newline_body_matches_parse_blocks():
    """Article row with single-newline body produces same blocks as parse_blocks."""
    import io

    import openpyxl

    from crosspost.scheduler.import_plan import build_plan, parse_rule, parse_table

    body = "Введение.\nhttps://example.com/img.jpg\nЗаключение."
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["тип", "файл", "текст", "заголовок"])
    ws.append(["статья", "", body, "Заголовок"])
    buf = io.BytesIO()
    wb.save(buf)

    rows = parse_table(buf.getvalue(), "plan.xlsx")
    rule = parse_rule("2025-01-01", "10:00")
    report = build_plan(rows, [], rule)

    assert not report.problems
    assert len(report.resolved) == 1
    rr = report.resolved[0]

    expected = parse_blocks(body)
    assert len(rr.blocks) == len(expected)
    for got, want in zip(rr.blocks, expected, strict=True):
        assert type(got) is type(want)
        if isinstance(want, ParagraphBlock):
            assert got.text == want.text
        else:
            assert got.url == want.url


# ── 5. Windows line endings (\r\n) ────────────────────────────────────────────


def test_windows_crlf_handled_as_single_newline():
    """\r\n line endings parse identically to \n."""
    text_lf = "Текст\nhttps://example.com/pic.jpg\nтекст"
    text_crlf = "Текст\r\nhttps://example.com/pic.jpg\r\nтекст"
    assert parse_blocks(text_lf) == parse_blocks(text_crlf)


def test_crlf_blank_line_dropped():
    """\r\n blank lines are dropped just like \n blank lines."""
    blocks = parse_blocks("A\r\n\r\nB")
    assert len(blocks) == 2
    assert blocks[0].text == "A"
    assert blocks[1].text == "B"
