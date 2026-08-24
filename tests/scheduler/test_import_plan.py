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
    analyze_headers,
    build_plan,
    detect_columns,
    natural_key,
    parse_rule,
    parse_table,
    peek_headers,
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


def test_detect_columns_normalizes_underscores_and_synonyms():
    # Подчёркивания → пробелы; расширенные синонимы (фото, имя_файла, описание, лонгрид).
    cols = detect_columns(["имя_файла", "Описание", "Лонгрид"])
    assert cols["file"] == 0
    assert cols["text"] == 1
    assert cols["article"] == 2


def test_detect_columns_photo_synonym():
    cols = detect_columns(["фото", "подпись"])
    assert cols == {"file": 0, "text": 1}


def test_detect_columns_mapping_override():
    # Заголовки не из словаря — но пользовательский маппинг {logical: header} их связывает.
    cols = detect_columns(["colA", "colB"], mapping={"file": "colA", "text": "colB"})
    assert cols == {"file": 0, "text": 1}


def test_peek_headers_csv_and_xlsx():
    assert peek_headers(_csv("A,B,C\n1,2,3\n"), "plan.csv") == ["A", "B", "C"]
    assert peek_headers(_xlsx([["X", "Y"], ["1", "2"]]), "plan.xlsx") == ["X", "Y"]


def test_analyze_headers_reports_missing_without_raising():
    # Нераспознанные колонки — НЕ падаем, а сообщаем, что не нашли.
    detected, missing = analyze_headers(["colA", "colB"])
    assert "file" in missing and "text" in missing
    assert detected == {}


def test_analyze_headers_partial_detection():
    detected, missing = analyze_headers(["файл", "colB"])
    assert detected.get("file") == 0
    assert missing == ["text"]


def test_analyze_headers_mapping_resolves_missing():
    detected, missing = analyze_headers(["colA", "colB"], mapping={"file": "colA", "text": "colB"})
    assert detected == {"file": 0, "text": 1}
    assert missing == []


def test_parse_table_with_mapping_override():
    rows = parse_table(
        _csv("colA,colB\npic.jpg,Привет\n"),
        "plan.csv",
        mapping={"file": "colA", "text": "colB"},
    )
    assert len(rows) == 1
    assert rows[0].file == "pic.jpg"
    assert rows[0].text == "Привет"


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


# ── Интервалы повторения ──────────────────────────────────────────────────────


def test_rule_daily_default():
    # По умолчанию — каждый день (обратная совместимость).
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    assert rule.slot_datetime(0) == datetime(2026, 9, 1, 9, 0)
    assert rule.slot_datetime(1) == datetime(2026, 9, 2, 9, 0)
    assert rule.slot_datetime(2) == datetime(2026, 9, 3, 9, 0)


def test_rule_every_n_days():
    # Через 3 дня: 1 → 4 → 7 сентября.
    rule = ScheduleRule(
        start_date=date(2026, 9, 1), times=(time(9, 0),),
        interval="every_n_days", interval_n=3,
    )
    assert rule.slot_datetime(0) == datetime(2026, 9, 1, 9, 0)
    assert rule.slot_datetime(1) == datetime(2026, 9, 4, 9, 0)
    assert rule.slot_datetime(2) == datetime(2026, 9, 7, 9, 0)


def test_rule_every_n_days_with_multiple_slots():
    # 2 слота в день, интервал 2 дня. День 0 = 1 сен (два слота), день 1 = 3 сен.
    rule = ScheduleRule(
        start_date=date(2026, 9, 1), times=(time(9, 0), time(21, 0)),
        interval="every_n_days", interval_n=2,
    )
    assert rule.slot_datetime(0) == datetime(2026, 9, 1, 9, 0)
    assert rule.slot_datetime(1) == datetime(2026, 9, 1, 21, 0)
    assert rule.slot_datetime(2) == datetime(2026, 9, 3, 9, 0)
    assert rule.slot_datetime(3) == datetime(2026, 9, 3, 21, 0)


def test_rule_weekly_selected_weekdays():
    # 2026-09-01 — вторник (weekday=1). Дни: пн(0), ср(2), пт(4).
    # Первый подходящий день ≥ старта: ср 2 сен, затем пт 4 сен, пн 7 сен, ср 9 сен.
    rule = ScheduleRule(
        start_date=date(2026, 9, 1), times=(time(12, 0),),
        interval="weekly", weekdays=(0, 2, 4),
    )
    assert rule.slot_datetime(0) == datetime(2026, 9, 2, 12, 0)
    assert rule.slot_datetime(1) == datetime(2026, 9, 4, 12, 0)
    assert rule.slot_datetime(2) == datetime(2026, 9, 7, 12, 0)
    assert rule.slot_datetime(3) == datetime(2026, 9, 9, 12, 0)


def test_rule_weekly_start_on_selected_day():
    # Старт в понедельник 2026-09-07 (weekday=0), дни пн+чт(3).
    rule = ScheduleRule(
        start_date=date(2026, 9, 7), times=(time(8, 0),),
        interval="weekly", weekdays=(0, 3),
    )
    assert rule.slot_datetime(0) == datetime(2026, 9, 7, 8, 0)   # пн
    assert rule.slot_datetime(1) == datetime(2026, 9, 10, 8, 0)  # чт
    assert rule.slot_datetime(2) == datetime(2026, 9, 14, 8, 0)  # следующий пн


def test_rule_monthly():
    # Раз в месяц: 15 янв → 15 фев → 15 мар.
    rule = ScheduleRule(
        start_date=date(2026, 1, 15), times=(time(10, 0),), interval="monthly",
    )
    assert rule.slot_datetime(0) == datetime(2026, 1, 15, 10, 0)
    assert rule.slot_datetime(1) == datetime(2026, 2, 15, 10, 0)
    assert rule.slot_datetime(2) == datetime(2026, 3, 15, 10, 0)


def test_rule_monthly_clamps_short_month():
    # 31 января + месяц → 28 февраля (2026 не високосный).
    rule = ScheduleRule(
        start_date=date(2026, 1, 31), times=(time(10, 0),), interval="monthly",
    )
    assert rule.slot_datetime(1) == datetime(2026, 2, 28, 10, 0)


def test_parse_rule_interval_params():
    rule = parse_rule("2026-09-01", "10:00", interval="every_n_days", interval_n=5)
    assert rule.interval == "every_n_days"
    assert rule.interval_n == 5


def test_parse_rule_weekly_requires_weekdays():
    with pytest.raises(PlanImportError, match="день недели"):
        parse_rule("2026-09-01", "10:00", interval="weekly", weekdays=())


def test_parse_rule_weekly_parses_weekdays():
    rule = parse_rule("2026-09-01", "10:00", interval="weekly", weekdays=(1, 3, 5))
    assert rule.interval == "weekly"
    assert rule.weekdays == (1, 3, 5)


def test_parse_rule_every_n_days_rejects_zero():
    with pytest.raises(PlanImportError, match="[Ии]нтервал"):
        parse_rule("2026-09-01", "10:00", interval="every_n_days", interval_n=0)


def test_build_plan_explicit_date_beats_interval_rule():
    # Явная дата в строке приоритетнее правила повтора.
    rows = [
        _row(1, "1.jpg", "Первый"),
        _row(2, "2.jpg", "Второй", d=date(2026, 12, 25), t=time(15, 0)),
        _row(3, "3.jpg", "Третий"),
    ]
    rule = ScheduleRule(
        start_date=date(2026, 9, 1), times=(time(9, 0),),
        interval="every_n_days", interval_n=7,
    )
    report = build_plan(rows, ["1.jpg", "2.jpg", "3.jpg"], rule)
    by_row = {r.row_number: r for r in report.resolved}
    assert by_row[2].scheduled_at == datetime(2026, 12, 25, 15, 0)  # явная
    # Правило раскидывает ТОЛЬКО строки без явной даты: 1.jpg → старт, 3.jpg → +7 дней.
    assert by_row[1].scheduled_at == datetime(2026, 9, 1, 9, 0)
    assert by_row[3].scheduled_at == datetime(2026, 9, 8, 9, 0)


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


# ── Блок 3: источник медиа в отчёте (media_source) ────────────────────────────


def test_build_plan_uploaded_file_source_is_upload():
    rows = [_row(1, "1.jpg", "Пост")]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, ["1.jpg"], rule)
    assert report.resolved[0].media_source == "upload"


def test_build_plan_http_url_is_remote_source_no_warning():
    rows = [_row(1, "https://example.com/pic.png", "Пост")]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, [], rule)
    assert report.warnings == []  # URL — валидный источник, не «не загружено»
    assert report.resolved[0].media_source == "http"
    assert report.resolved[0].file == "https://example.com/pic.png"


def test_build_plan_gdrive_is_problem_and_yadisk_is_supported():
    rows = [
        _row(1, "https://drive.google.com/file/d/ABC/view", "GD", d=date(2026, 5, 1)),
        _row(2, "https://disk.yandex.ru/i/xyz", "YD", d=date(2026, 5, 1)),
    ]
    report = build_plan(rows, [], rule=None)
    assert [(p.row_number, p.reason) for p in report.problems] == [
        (1, "Google Drive не поддерживается. Используйте прямую HTTP-ссылку или Яндекс.Диск.")
    ]
    assert [(r.text, r.media_source) for r in report.resolved] == [("YD", "yadisk")]


def test_build_plan_filename_from_folder_when_configured():
    rows = [_row(1, "sub/01.jpg", "Из папки")]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, [], rule, folder_configured=True)
    assert report.warnings == []  # папка задана → не предупреждаем
    assert report.resolved[0].media_source == "folder"


def test_build_plan_filename_no_upload_no_folder_still_warns():
    rows = [_row(1, "01.jpg", "Без источника")]
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan(rows, [], rule, folder_configured=False)
    assert len(report.warnings) == 1
    assert report.resolved[0].media_source == ""


# ── БЛОК 1: сквозное поле article_text (лонгрид для Telegraph) ─────────────────


def test_detect_columns_article_synonyms():
    from crosspost.scheduler.import_plan import detect_columns

    for header in (
        ["файл", "текст", "статья"],
        ["file", "text", "article"],
        ["файл", "текст", "лонгрид"],
    ):
        cols = detect_columns(header)
        assert "article" in cols


def test_parse_csv_reads_article_column():
    data = "файл,текст,статья\n1.jpg,Короткий,Полное тело лонгрида\n".encode()
    rows = parse_table(data, "plan.csv")
    assert rows[0].article_text == "Полное тело лонгрида"


def test_parse_csv_article_column_absent_defaults_empty():
    data = "файл,текст\n1.jpg,Только пост\n".encode()
    rows = parse_table(data, "plan.csv")
    assert rows[0].article_text == ""


def test_build_plan_threads_article_text_rule():
    row = PlanRow(row_number=1, file="1.jpg", text="Короткий", article_text="Лонгрид")
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan([row], ["1.jpg"], rule)
    assert report.resolved[0].article_text == "Лонгрид"


def test_build_plan_threads_article_text_explicit():
    row = PlanRow(
        row_number=1,
        file="1.jpg",
        text="Короткий",
        article_text="Лонгрид",
        explicit_date=date(2026, 12, 1),
    )
    report = build_plan([row], ["1.jpg"], rule=None)
    assert report.resolved[0].article_text == "Лонгрид"


def test_build_plan_no_article_defaults_empty():
    row = PlanRow(row_number=1, file="1.jpg", text="Только пост")
    rule = ScheduleRule(start_date=date(2026, 9, 1), times=(time(9, 0),))
    report = build_plan([row], ["1.jpg"], rule)
    assert report.resolved[0].article_text == ""
