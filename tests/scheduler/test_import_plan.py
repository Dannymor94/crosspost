"""Тесты чистой логики импорта контент-плана. Без HTTP/БД.

Покрытие: парсинг CSV/XLSX, гибкие колонки, связывание с файлами,
несвязанные строки, авто-раскидка по правилу, натуральная сортировка,
приоритет явной даты над правилом.
"""

from __future__ import annotations

import io
from datetime import date, datetime, time

import pytest

from crosspost.scheduler.import_plan import (
    PlanImportError,
    PlanRow,
    ScheduleRule,
    build_plan,
    detect_columns,
    natural_key,
    parse_rule,
    parse_table,
)

# ── Хелперы ───────────────────────────────────────────────────────────────────


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


def _xlsx(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Распознавание колонок ─────────────────────────────────────────────────────


def test_detect_columns_flexible_synonyms_and_case():
    cols = detect_columns(["Файл", "ТЕКСТ", "Дата", "Время"])
    assert cols == {"file": 0, "text": 1, "date": 2, "time": 3}


def test_detect_columns_english_synonyms():
    cols = detect_columns(["image", "caption"])
    assert cols["file"] == 0
    assert cols["text"] == 1
    assert "date" not in cols


def test_detect_columns_missing_file_raises():
    with pytest.raises(PlanImportError, match="имен"):
        detect_columns(["текст", "дата"])


def test_detect_columns_missing_text_raises():
    with pytest.raises(PlanImportError, match="текст"):
        detect_columns(["файл", "дата"])


# ── Парсинг CSV ───────────────────────────────────────────────────────────────


def test_parse_csv_basic():
    data = _csv("файл,текст,дата,время\n01.jpg,Привет,2026-09-01,10:00\n")
    rows = parse_table(data, "plan.csv")
    assert len(rows) == 1
    r = rows[0]
    assert r.file == "01.jpg"
    assert r.text == "Привет"
    assert r.explicit_date == date(2026, 9, 1)
    assert r.explicit_time == time(10, 0)


def test_parse_csv_optional_columns_absent():
    data = _csv("файл,текст\n02.jpg,Без даты\n")
    rows = parse_table(data, "plan.csv")
    assert rows[0].explicit_date is None
    assert rows[0].explicit_time is None


def test_parse_csv_semicolon_delimiter():
    data = _csv("файл;текст\n03.jpg;С точкой с запятой\n")
    rows = parse_table(data, "plan.csv")
    assert rows[0].file == "03.jpg"
    assert rows[0].text == "С точкой с запятой"


def test_parse_csv_skips_fully_empty_rows():
    data = _csv("файл,текст\n01.jpg,А\n\n02.jpg,Б\n")
    rows = parse_table(data, "plan.csv")
    assert len(rows) == 2


def test_parse_csv_bad_date_raises():
    data = _csv("файл,текст,дата\n01.jpg,А,31-02-9999x\n")
    with pytest.raises(PlanImportError, match="дат"):
        parse_table(data, "plan.csv")


def test_parse_unknown_extension_raises():
    with pytest.raises(PlanImportError, match="csv"):
        parse_table(b"x", "plan.txt")


# ── Парсинг XLSX ──────────────────────────────────────────────────────────────


def test_parse_xlsx_basic():
    data = _xlsx(
        [
            ["файл", "текст", "дата", "время"],
            ["01.jpg", "Пост 1", "2026-09-01", "09:30"],
            ["02.jpg", "Пост 2", None, None],
        ]
    )
    rows = parse_table(data, "plan.xlsx")
    assert len(rows) == 2
    assert rows[0].file == "01.jpg"
    assert rows[0].explicit_date == date(2026, 9, 1)
    assert rows[0].explicit_time == time(9, 30)
    assert rows[1].explicit_date is None


def test_parse_xlsx_native_datetime_cells():
    """Excel может отдать дату/время как native datetime — тоже должно разбираться."""
    data = _xlsx(
        [
            ["файл", "текст", "дата", "время"],
            ["01.jpg", "Пост", datetime(2026, 9, 1, 0, 0), datetime(2026, 1, 1, 14, 15)],
        ]
    )
    rows = parse_table(data, "plan.xlsx")
    assert rows[0].explicit_date == date(2026, 9, 1)
    assert rows[0].explicit_time == time(14, 15)


# ── Натуральная сортировка ────────────────────────────────────────────────────


def test_natural_key_orders_numerically():
    names = ["10.jpg", "2.jpg", "1.jpg", "21.jpg"]
    assert sorted(names, key=natural_key) == ["1.jpg", "2.jpg", "10.jpg", "21.jpg"]


# ── Правило раскидки ──────────────────────────────────────────────────────────


def test_parse_rule_basic():
    rule = parse_rule("2026-09-01", "10:00, 18:00")
    assert rule.start_date == date(2026, 9, 1)
    assert rule.times == (time(10, 0), time(18, 0))


def test_parse_rule_no_times_raises():
    with pytest.raises(PlanImportError, match="время"):
        parse_rule("2026-09-01", "")


def test_rule_slot_datetime_distributes_across_days():
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(10, 0), time(18, 0)))
    assert rule.slot_datetime(0) == datetime(2026, 9, 1, 10, 0)
    assert rule.slot_datetime(1) == datetime(2026, 9, 1, 18, 0)
    assert rule.slot_datetime(2) == datetime(2026, 9, 2, 10, 0)
    assert rule.slot_datetime(3) == datetime(2026, 9, 2, 18, 0)


# ── build_plan: связывание + резолвинг ────────────────────────────────────────


def _row(n, file, text, d=None, t=None):
    return PlanRow(row_number=n, file=file, text=text, explicit_date=d, explicit_time=t)


def test_build_plan_links_and_applies_rule_in_natural_order():
    rows = [
        _row(1, "10.jpg", "Десятый"),
        _row(2, "2.jpg", "Второй"),
        _row(3, "1.jpg", "Первый"),
    ]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, ["1.jpg", "2.jpg", "10.jpg"], rule)

    assert report.matched_count == 3
    assert not report.problems
    # Порядок по времени = натуральный порядок имён: 1 → 2 → 10.
    order = [(r.file, r.scheduled_at) for r in report.resolved]
    assert order == [
        ("1.jpg", datetime(2026, 9, 1, 9, 0)),
        ("2.jpg", datetime(2026, 9, 2, 9, 0)),
        ("10.jpg", datetime(2026, 9, 3, 9, 0)),
    ]
    assert all(r.source == "rule" for r in report.resolved)


def test_build_plan_explicit_date_takes_priority_over_rule():
    rows = [
        _row(1, "a.jpg", "Явная", d=date(2026, 12, 31), t=time(23, 0)),
        _row(2, "b.jpg", "По правилу"),
    ]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, ["a.jpg", "b.jpg"], rule)

    by_file = {r.file: r for r in report.resolved}
    assert by_file["a.jpg"].scheduled_at == datetime(2026, 12, 31, 23, 0)
    assert by_file["a.jpg"].source == "explicit"
    assert by_file["b.jpg"].scheduled_at == datetime(2026, 9, 1, 9, 0)
    assert by_file["b.jpg"].source == "rule"


def test_build_plan_explicit_date_without_time_uses_first_rule_time():
    rows = [_row(1, "a.jpg", "Дата без времени", d=date(2026, 10, 5))]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(8, 30), time(20, 0)))
    report = build_plan(rows, ["a.jpg"], rule)
    assert report.resolved[0].scheduled_at == datetime(2026, 10, 5, 8, 30)


def test_build_plan_reports_unmatched_files():
    rows = [_row(1, "used.jpg", "Есть строка")]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, ["used.jpg", "orphan1.jpg", "orphan2.jpg"], rule)
    assert report.unmatched_files == ["orphan1.jpg", "orphan2.jpg"]


def test_build_plan_missing_file_becomes_warning_not_problem():
    """Файл указан, но не загружен → предупреждение, пост всё равно планируется (без фото)."""
    rows = [_row(1, "missing.jpg", "Нет картинки")]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, ["other.jpg"], rule)
    assert report.matched_count == 1  # пост запланирован
    assert len(report.problems) == 0  # не ошибка
    assert len(report.warnings) == 1
    assert report.warnings[0].row_number == 1
    assert "без фото" in report.warnings[0].reason
    assert report.resolved[0].file == ""  # файл не привязан


def test_build_plan_reports_empty_text_and_empty_file():
    rows = [
        _row(1, "", "Текст без файла"),  # пустой файл → текстовый пост, OK
        _row(2, "x.jpg", ""),  # пустой текст → ошибка
    ]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, ["x.jpg"], rule)
    # строка 1: пустой файл теперь допускается (текстовый пост)
    assert report.matched_count == 1
    assert len(report.problems) == 1
    assert report.problems[0].row_number == 2
    assert "пустой текст" in report.problems[0].reason


def test_build_plan_case_insensitive_filename_match():
    rows = [_row(1, "Photo.JPG", "Регистр")]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, ["photo.jpg"], rule)
    assert report.matched_count == 1


def test_build_plan_flexible_number_match():
    """'1.jpg' в CSV совпадает с 'cover_1.png' по первому числу в имени."""
    rows = [
        _row(1, "1.jpg", "Первый"),
        _row(2, "2.jpg", "Второй"),
        _row(3, "10.jpg", "Десятый"),
    ]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, ["cover_1.png", "photo_2.jpg", "image_10.png"], rule)
    assert report.matched_count == 3
    assert len(report.warnings) == 0
    assert len(report.problems) == 0
    # Файл совпал по числу, не по имени
    assert report.resolved[0].file == "cover_1.png"
    assert report.resolved[1].file == "photo_2.jpg"
    assert report.resolved[2].file == "image_10.png"


def test_build_plan_exact_match_wins_over_flex():
    """Точное совпадение имени приоритетнее гибкого числового."""
    rows = [_row(1, "1.jpg", "Точное совпадение")]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    # Оба файла содержат '1', но '1.jpg' — точное совпадение
    report = build_plan(rows, ["1.jpg", "cover_1.png"], rule)
    assert report.matched_count == 1
    assert report.resolved[0].file == "1.jpg"


def test_build_plan_no_rule_and_no_date_is_a_problem():
    rows = [_row(1, "a.jpg", "Без даты и без правила")]
    report = build_plan(rows, ["a.jpg"], rule=None)
    assert report.matched_count == 0
    assert "правил" in report.problems[0].reason


def test_build_plan_all_explicit_needs_no_rule():
    rows = [_row(1, "a.jpg", "Явная", d=date(2026, 5, 1), t=time(12, 0))]
    report = build_plan(rows, ["a.jpg"], rule=None)
    assert report.matched_count == 1
    assert report.resolved[0].scheduled_at == datetime(2026, 5, 1, 12, 0)
