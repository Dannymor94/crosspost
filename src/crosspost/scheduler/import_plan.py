"""Массовый импорт контент-плана из таблицы (CSV/XLSX). Трек A, часть 1.

Чистая логика — без HTTP и БД, полностью тестируется на данных:
  1. parse_table  — байты таблицы → строки плана (гибкое распознавание колонок).
  2. link_files   — связать строки с загруженными картинками по имени файла.
  3. resolve      — назначить время: явная дата в строке ИЛИ правило раскидки.
  4. build_plan   — оркестрация 1-3 → отчёт (что связалось, что нет, превью времени).

Порядок постов без явной даты — по имени файла, НАТУРАЛЬНОЙ сортировкой
(2.jpg < 10.jpg). Явные дата/время в строке имеют приоритет над правилом.

Ошибки на уровне строки НЕ роняют весь импорт — строка уходит в problems с
человеческой причиной. Ошибка на уровне таблицы (нет обязательной колонки) —
исключение PlanImportError.
"""

from __future__ import annotations

import asyncio
import calendar
import csv
import io
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal

from crosspost.scheduler.media_sources import (
    MediaSourceError,
    SourceKind,
    classify_source,
    fetch_media,
)

# ── Plan image resolution settings (env-configurable, consistent with SCHEDULER_* naming) ──
_IMPORT_PACING_SECONDS = float(os.getenv("IMPORT_PACING_SECONDS", "2.0"))
_IMPORT_MAX_CONCURRENCY = int(os.getenv("IMPORT_MAX_CONCURRENCY", "3"))
_IMPORT_MAX_IMAGES = int(os.getenv("IMPORT_MAX_IMAGES", "200"))
_IMPORT_MAX_BYTES = int(os.getenv("IMPORT_MAX_BYTES", str(50 * 1024 * 1024)))  # 50 MB total
_IMPORT_IMAGE_MAX_BYTES = int(
    os.getenv("IMPORT_IMAGE_MAX_BYTES", str(5 * 1024 * 1024))
)  # 5 MB per image

# Patchable sleep — tests monkeypatch crosspost.scheduler.import_plan._plan_sleep.
_plan_sleep = asyncio.sleep

# ── Ошибки ────────────────────────────────────────────────────────────────────


class PlanImportError(ValueError):
    """Таблица непригодна целиком (нет обязательной колонки, пустой файл, битый формат)."""


# ── Распознавание колонок (гибко: регистр + синонимы) ─────────────────────────

_COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "file": (
        "файл",
        "file",
        "image",
        "изображение",
        "картинка",
        "фото",
        "img",
        "имя файла",
        "filename",
    ),
    "text": ("текст", "text", "подпись", "caption", "описание", "content", "пост"),
    "date": ("дата", "date", "день"),
    "time": ("время", "time", "час"),
    "article": ("статья", "article", "лонгрид", "longread", "тело"),
    "type": ("тип", "type", "тип контента", "content type", "вид", "kind"),
    "title": ("заголовок", "title", "название", "heading"),
}

# Обязательные логические колонки (без них план не собрать без ручного маппинга).
REQUIRED_COLUMNS: tuple[str, ...] = ("file", "text")


def _normalize_header(cell: str) -> str:
    """Заголовок → канонический вид: нижний регистр, _→пробел, схлопнутые пробелы."""
    s = (cell or "").strip().lower().replace("_", " ")
    return re.sub(r"\s+", " ", s)


def detect_columns(header: list[str], *, mapping: dict[str, str] | None = None) -> dict[str, int]:
    """header → {logical_name: column_index}. Регистр/пробелы/подчёркивания игнорируются.

    mapping — пользовательское сопоставление {logical: заголовок}, перекрывает
    авто-распознавание (для нераспознанных колонок). file и text обязательны.
    """
    result = _resolve_columns(header, mapping)
    if "file" not in result:
        raise PlanImportError(
            "Не найдена колонка с именем файла (ожидается «файл», «file» или «image»)."
        )
    if "text" not in result:
        raise PlanImportError(
            "Не найдена колонка с текстом (ожидается «текст», «text» или «подпись»)."
        )
    return result


def _resolve_columns(header: list[str], mapping: dict[str, str] | None) -> dict[str, int]:
    """Общее ядро распознавания без падения: авто-синонимы + пользовательский маппинг."""
    normalized = [_normalize_header(h) for h in header]
    result: dict[str, int] = {}
    for logical, synonyms in _COLUMN_SYNONYMS.items():
        norm_syns = {_normalize_header(s) for s in synonyms}
        for idx, cell in enumerate(normalized):
            if cell in norm_syns:
                result[logical] = idx
                break
    # Пользовательский маппинг перекрывает авто-распознавание.
    for logical, chosen in (mapping or {}).items():
        want = _normalize_header(chosen)
        for idx, cell in enumerate(normalized):
            if cell == want:
                result[logical] = idx
                break
    return result


def analyze_headers(
    header: list[str], *, mapping: dict[str, str] | None = None
) -> tuple[dict[str, int], list[str]]:
    """Распознать колонки БЕЗ падения. Вернуть (detected, missing_required).

    Для UI ручного маппинга: показать, что нашли и каких обязательных не хватает.
    """
    detected = _resolve_columns(header, mapping)
    missing = [c for c in REQUIRED_COLUMNS if c not in detected]
    return detected, missing


# ── Модель строки плана ───────────────────────────────────────────────────────


@dataclass
class PlanRow:
    """Одна сырая строка плана (до связывания и раскидки)."""

    row_number: int  # 1-based номер строки данных (для сообщений об ошибках)
    file: str
    text: str
    article_text: str = ""  # тело лонгрида для Telegraph; пусто → обычный пост
    explicit_date: date | None = None
    explicit_time: time | None = None
    content_type: str = "post"  # "post" | "article"
    title: str = ""  # article title (required for article rows)


# ── Парсинг даты/времени из ячеек ─────────────────────────────────────────────


def _parse_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)  # YYYY-MM-DD
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", s)  # DD.MM.YYYY
    if m:
        d, mo, y = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError as exc:
            raise PlanImportError(f"Некорректная дата «{s}»") from exc
    raise PlanImportError(f"Не разобрать дату «{s}» (ожидается YYYY-MM-DD).")


def _parse_time(value: object) -> time | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    s = str(value).strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)  # HH:MM
    if not m:
        raise PlanImportError(f"Не разобрать время «{s}» (ожидается HH:MM).")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise PlanImportError(f"Некорректное время «{s}».")
    return time(hh, mm)


# ── Парсинг таблицы (CSV / XLSX) ──────────────────────────────────────────────


def _read_rows(data: bytes, filename: str) -> list[list]:
    """Байты таблицы → сырые строки. Формат выбирается по расширению filename."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return _read_csv(data)
    if name.endswith((".xlsx", ".xlsm")):
        return _read_xlsx(data)
    raise PlanImportError("Поддерживаются только файлы .csv и .xlsx")


def peek_headers(data: bytes, filename: str) -> list[str]:
    """Заголовки таблицы (первая строка) как строки — для UI ручного маппинга."""
    raw_rows = _read_rows(data, filename)
    if not raw_rows:
        raise PlanImportError("Таблица пустая.")
    return [str(c) if c is not None else "" for c in raw_rows[0]]


def parse_table(
    data: bytes, filename: str, *, mapping: dict[str, str] | None = None
) -> list[PlanRow]:
    """Байты таблицы → список PlanRow. Формат выбирается по расширению filename.

    mapping — пользовательское сопоставление {logical: заголовок}, применяется
    поверх авто-распознавания (для колонок, не угаданных по синонимам).
    """
    raw_rows = _read_rows(data, filename)
    if not raw_rows:
        raise PlanImportError("Таблица пустая.")

    header, *body = raw_rows
    cols = detect_columns([str(c) if c is not None else "" for c in header], mapping=mapping)

    rows: list[PlanRow] = []
    for i, raw in enumerate(body, start=1):

        def cell(logical: str, _raw=raw) -> object:
            idx = cols.get(logical)
            if idx is None or idx >= len(_raw):
                return None
            return _raw[idx]

        file_val = cell("file")
        text_val = cell("text")
        article_val = cell("article")
        type_val = cell("type")
        title_val = cell("title")
        file_str = "" if file_val is None else str(file_val).strip()
        text_str = "" if text_val is None else str(text_val).strip()
        article_str = "" if article_val is None else str(article_val).strip()
        type_str = "" if type_val is None else str(type_val).strip().lower()
        title_str = "" if title_val is None else str(title_val).strip()

        # Нормализация типа контента. Пустая ячейка → post.
        _TYPE_MAP = {
            "": "post",
            "пост": "post",
            "post": "post",
            "статья": "article",
            "article": "article",
        }
        content_type = _TYPE_MAP.get(type_str)  # None = неизвестный тип

        # Полностью пустая строка — молча пропускаем (хвост таблицы).
        if not file_str and not text_str:
            continue

        rows.append(
            PlanRow(
                row_number=i,
                file=file_str,
                text=text_str,
                article_text=article_str,
                explicit_date=_parse_date(cell("date")),
                explicit_time=_parse_time(cell("time")),
                content_type=content_type or type_str,  # preserve raw if unknown
                title=title_str,
            )
        )
    return rows


def _read_csv(data: bytes) -> list[list]:
    # Пробуем UTF-8 (с BOM), затем cp1251 — типичный экспорт Windows/Excel на ru-локали.
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8-sig", errors="replace")
    # Определяем разделитель (запятая или точка с запятой — частый экспорт из Excel).
    sample = text[:2048]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [row for row in reader if any((c or "").strip() for c in row)]


def _read_xlsx(data: bytes) -> list[list]:
    from openpyxl import load_workbook  # локальный импорт — тяжёлая зависимость

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    # Отбрасываем полностью пустые строки.
    return [r for r in rows if any(c is not None and str(c).strip() for c in r)]


# ── Правило раскидки по времени ───────────────────────────────────────────────


Interval = Literal["daily", "every_n_days", "weekly", "monthly"]


def _add_months(d: date, months: int) -> date:
    """Прибавить months к дате, зажимая день под длину месяца (31 янв +1 → 28/29 фев)."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class ScheduleRule:
    """Правило авто-раскидки строк без явной даты.

    times — времена постинга в течение дня (по порядку); слотов в день = len(times).
    interval — периодичность «дней постинга»:
      daily         — каждый день;
      every_n_days  — через interval_n дней;
      weekly        — только в выбранные дни недели (weekdays, 0=пн..6=вс);
      monthly       — раз в месяц (то же число).
    Пост i (в натуральном порядке имён) → день постинга i // len(times),
    слот времени i % len(times). Окончание — по количеству постов (все строки плана).
    """

    start_date: date
    times: tuple[time, ...]
    interval: Interval = "daily"
    interval_n: int = 1
    weekdays: tuple[int, ...] = ()

    def _nth_day(self, k: int) -> date:
        """k-й (0-based) день постинга по интервалу."""
        if self.interval == "every_n_days":
            return self.start_date + timedelta(days=k * self.interval_n)
        if self.interval == "monthly":
            return _add_months(self.start_date, k)
        if self.interval == "weekly":
            wd = set(self.weekdays)
            d = self.start_date
            count = 0
            # Плановые размеры малы (десятки-сотни постов) → простой перебор дат.
            # ponytail: линейный перебор, дней плана немного
            while True:
                if d.weekday() in wd:
                    if count == k:
                        return d
                    count += 1
                d += timedelta(days=1)
        # daily
        return self.start_date + timedelta(days=k)

    def slot_datetime(self, ordinal: int) -> datetime:
        """ordinal (0-based) → дата/время слота."""
        per_day = len(self.times)
        day_index = ordinal // per_day
        slot = ordinal % per_day
        return datetime.combine(self._nth_day(day_index), self.times[slot])


def parse_rule(
    start_date: str,
    times_csv: str,
    *,
    interval: str = "daily",
    interval_n: int = 1,
    weekdays: tuple[int, ...] = (),
) -> ScheduleRule:
    """UI-ввод правила → ScheduleRule. start_date=YYYY-MM-DD; times='10:00, 18:00'."""
    try:
        sd = date.fromisoformat(start_date.strip())
    except (ValueError, AttributeError) as exc:
        raise PlanImportError("Дата начала: ожидается YYYY-MM-DD.") from exc
    times: list[time] = []
    for part in (times_csv or "").split(","):
        part = part.strip()
        if not part:
            continue
        t = _parse_time(part)
        if t is not None:
            times.append(t)
    if not times:
        raise PlanImportError("Укажите хотя бы одно время постинга (напр. 10:00, 18:00).")

    if interval not in ("daily", "every_n_days", "weekly", "monthly"):
        raise PlanImportError(f"Неизвестный интервал повтора: {interval!r}.")
    if interval == "every_n_days" and interval_n < 1:
        raise PlanImportError("Интервал в днях должен быть ≥ 1.")
    if interval == "weekly":
        if not weekdays:
            raise PlanImportError("Выберите хотя бы один день недели.")
        if any(w < 0 or w > 6 for w in weekdays):
            raise PlanImportError("День недели: ожидается 0 (пн) … 6 (вс).")

    return ScheduleRule(
        start_date=sd,
        times=tuple(times),
        interval=interval,  # type: ignore[arg-type]
        interval_n=interval_n,
        weekdays=tuple(sorted(set(weekdays))),
    )


# ── Натуральная сортировка по имени файла ─────────────────────────────────────


def natural_key(name: str) -> list:
    """Ключ натуральной сортировки: '2.jpg' < '10.jpg'. Числа сравниваются как числа."""
    parts = re.split(r"(\d+)", name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


# ── Связывание + резолвинг времени ────────────────────────────────────────────


@dataclass
class ResolvedRow:
    """Строка, готовая к планированию: связана с файлом, время назначено."""

    row_number: int
    file: str
    text: str
    scheduled_at: datetime
    source: Literal["explicit", "rule"]
    article_text: str = ""  # тело лонгрида для Telegraph; пусто → обычный пост
    # Откуда возьмётся картинка (для отчёта связывания, Блок 3):
    # "upload" | "folder" | "http" | "yadisk" | "" (нет файла).
    media_source: str = ""
    content_type: str = "post"  # "post" | "article"
    title: str = ""  # article title (required for articles)
    blocks: list | None = None  # article_blocks.Block list; None for posts


@dataclass
class RowProblem:
    """Строка, которую нельзя запланировать, с человеческой причиной."""

    row_number: int
    file: str
    reason: str


@dataclass
class ImportReport:
    """Итог импорта: что связалось (с временем), что нет, какие файлы лишние."""

    resolved: list[ResolvedRow] = field(default_factory=list)
    problems: list[RowProblem] = field(default_factory=list)
    warnings: list[RowProblem] = field(default_factory=list)  # файл не найден → текст без картинки
    unmatched_files: list[str] = field(default_factory=list)  # картинки без строки в плане

    @property
    def matched_count(self) -> int:
        return len(self.resolved)


def _first_number(name: str) -> int | None:
    """Первое число в имени файла: 'cover_1.png' → 1, '02.jpg' → 2, 'photo' → None."""
    m = re.search(r"\d+", _basename(name))
    return int(m.group()) if m else None


def build_plan(
    rows: list[PlanRow],
    available_files: list[str] | set[str],
    rule: ScheduleRule | None,
    *,
    folder_configured: bool = False,
) -> ImportReport:
    """Связать строки с файлами и назначить время. Не пишет ничего — только считает.

    available_files — имена загруженных картинок (сопоставление по basename,
    регистронезависимо). Сопоставление двухэтапное:
      1. Точное совпадение имени (регистронезависимо).
      2. Гибкое: первое число в имени файла CSV = первое число в имени загруженного файла
         (например, '1.jpg' совпадает с 'cover_1.png').
    rule может быть None, если ВСЕ строки с явной датой.

    Блок 3: значение колонки «файл» может быть не только именем загруженной картинки,
    но и прямой HTTP-ссылкой или публичной ссылкой Яндекс.Диска — это валидные
    источники без предупреждения «не загружено». Google Drive не поддерживается и
    попадает в problems уже на preview. Для имени файла источником может быть папка
    (folder_configured=True). media_source в каждой строке говорит, откуда возьмётся
    картинка; фактическое скачивание — на этапе commit.
    """
    # Индекс точного совпадения: lower(basename) → оригинальное имя.
    avail_index: dict[str, str] = {}
    # Индекс гибкого совпадения: первое число → оригинальное имя (первый wins).
    number_index: dict[int, str] = {}
    for name in available_files:
        base = _basename(name)
        avail_index.setdefault(base.lower(), name)
        n = _first_number(base)
        if n is not None:
            number_index.setdefault(n, name)

    report = ImportReport()
    used_files: set[str] = set()
    # (row, matched_name, media_source)
    rule_candidates: list[tuple[PlanRow, str, str]] = []

    for row in rows:
        # Unknown content_type → row-level error.
        if row.content_type not in ("post", "article"):
            report.problems.append(
                RowProblem(
                    row.row_number,
                    row.file,
                    f"неизвестный тип контента «{row.content_type}» (ожидается «пост» или «статья»)",
                )
            )
            continue

        if not row.text:
            report.problems.append(RowProblem(row.row_number, row.file, "пустой текст поста"))
            continue

        # Article-specific validation and block parsing.
        if row.content_type == "article":
            if not row.title:
                report.problems.append(
                    RowProblem(
                        row.row_number,
                        row.file,
                        "статья: заголовок обязателен (колонка «заголовок» / «title» пуста)",
                    )
                )
                continue

            from crosspost.content.article_blocks import parse_blocks

            blocks = parse_blocks(row.text)

            # File column is meaningless for articles: article images come only from
            # http(s) URL blocks inside the body (parse_blocks / _is_sole_url).
            # Report the ignored filename explicitly rather than silently dropping it.
            if row.file:
                report.warnings.append(
                    RowProblem(
                        row.row_number,
                        row.file,
                        f"статья: колонка «файл» игнорируется (значение «{row.file}»); "
                        "картинки статьи берутся только из URL внутри тела",
                    )
                )

            t = row.explicit_time or (rule.times[0] if rule else time(12, 0))
            if row.explicit_date is not None:
                report.resolved.append(
                    ResolvedRow(
                        row.row_number,
                        "",
                        row.text,
                        datetime.combine(row.explicit_date, t),
                        source="explicit",
                        content_type="article",
                        title=row.title,
                        blocks=blocks,
                    )
                )
            else:
                rule_candidates.append((row, "", ""))
                # Store blocks so _build_resolved_row can use them later.
                # We attach blocks to the row object directly as a transient attribute.
                row._blocks = blocks  # type: ignore[attr-defined]
            continue

        matched: str
        msource: str
        if not row.file:
            matched, msource = "", ""  # нет колонки файла — текстовый пост без предупреждения
        elif classify_source(row.file) is SourceKind.GDRIVE:
            report.problems.append(
                RowProblem(
                    row.row_number,
                    row.file,
                    "Google Drive не поддерживается. Используйте прямую HTTP-ссылку или Яндекс.Диск.",
                )
            )
            continue
        elif classify_source(row.file) is not SourceKind.FILENAME:
            # Прямая HTTP-ссылка или Яндекс.Диск — удалённый источник, скачается в commit.
            matched = row.file
            msource = classify_source(row.file).value
        else:
            row_base = _basename(row.file)
            exact = avail_index.get(row_base.lower())
            if exact is not None:
                matched, msource = exact, "upload"
                used_files.add(matched)
            else:
                # Гибкое сопоставление по первому числу в имени.
                n = _first_number(row_base)
                flex = number_index.get(n) if n is not None else None
                if flex is not None:
                    matched, msource = flex, "upload"
                    used_files.add(matched)
                elif folder_configured:
                    # Не загружено вручную, но задана папка-источник — возьмём из неё.
                    matched, msource = row.file, "folder"
                else:
                    # Ни загрузки, ни папки — текстовый пост + предупреждение.
                    report.warnings.append(
                        RowProblem(
                            row.row_number, row.file, "картинка не загружена — пост выйдет без фото"
                        )
                    )
                    matched, msource = "", ""

        if row.explicit_date is not None:
            # Явная дата — приоритет над правилом. Время: из строки, иначе первое
            # время правила, иначе полдень (детерминированный дефолт).
            t = row.explicit_time or (rule.times[0] if rule else time(12, 0))
            report.resolved.append(
                ResolvedRow(
                    row.row_number,
                    matched,
                    row.text,
                    datetime.combine(row.explicit_date, t),
                    source="explicit",
                    article_text=row.article_text,
                    media_source=msource,
                    content_type=row.content_type,
                    title=row.title,
                )
            )
        else:
            rule_candidates.append((row, matched, msource))

    # Строки без даты — раскидываем правилом в натуральном порядке имён файлов.
    if rule_candidates:
        if rule is None:
            for row, _, _ in rule_candidates:
                report.problems.append(
                    RowProblem(
                        row.row_number, row.file, "нет даты в строке и не задано правило раскидки"
                    )
                )
        else:
            # Сортируем по оригинальному имени из CSV (rc[0].file), а не по matched —
            # иначе гибкое совпадение 'cover_1.png' ломает порядок '1.jpg < 2.jpg < 10.jpg'.
            rule_candidates.sort(key=lambda rc: natural_key(rc[0].file or rc[1]))
            for ordinal, (row, matched, msource) in enumerate(rule_candidates):
                row_blocks = getattr(row, "_blocks", None)
                report.resolved.append(
                    ResolvedRow(
                        row.row_number,
                        matched,
                        row.text,
                        rule.slot_datetime(ordinal),
                        source="rule",
                        article_text=row.article_text,
                        media_source=msource,
                        content_type=row.content_type,
                        title=row.title,
                        blocks=row_blocks,
                    )
                )

    # Картинки, на которые не сослалась ни одна строка.
    report.unmatched_files = sorted(
        (name for name in available_files if name not in used_files),
        key=natural_key,
    )
    # Превью — по времени публикации.
    report.resolved.sort(key=lambda r: r.scheduled_at)
    return report


def _basename(path: str) -> str:
    """Только имя файла, без каталогов (терпимо к / и \\)."""
    return re.split(r"[\\/]", path.strip())[-1]


# ── Plan-level image resolution ───────────────────────────────────────────────


async def resolve_plan_images(
    report: ImportReport,
    import_key: str,
    *,
    client=None,
    pacing: float | None = None,
    max_concurrency: int | None = None,
    max_images: int | None = None,
    max_bytes: int | None = None,
) -> ImportReport:
    """Resolve image URLs in article blocks for all resolved article rows.

    Fail-closed policy (deliberate): a row with ANY failed image block is moved
    to problems and not imported. This mirrors the fail-closed choice already made
    for Telegraph+Telegram linking — if the article cannot be fully assembled, the
    whole article row is rejected rather than imported with holes.

    Deduplication: the same URL across rows/blocks is downloaded once and the stored
    path is shared. All images land under import_key so pub_repo.media_key_shared
    (the existing mechanism) prevents premature cleanup while other scheduled posts
    from the same import still reference that key.

    Bounds (all env-configurable, consistent with SCHEDULER_* naming):
      IMPORT_PACING_SECONDS   — delay between downloads (default 2 s)
      IMPORT_MAX_CONCURRENCY  — max simultaneous downloads (default 3)
      IMPORT_MAX_IMAGES       — unique-URL ceiling; exceeded → explicit report entry
      IMPORT_MAX_BYTES        — total bytes ceiling; exceeded → explicit report entry
      IMPORT_IMAGE_MAX_BYTES  — per-image cap passed to fetch_media (default 5 MB)
    """
    from crosspost.content.article_blocks import ImageBlock
    from crosspost.web.media import save_binaries

    _pacing = pacing if pacing is not None else _IMPORT_PACING_SECONDS
    _max_conc = max_concurrency if max_concurrency is not None else _IMPORT_MAX_CONCURRENCY
    _max_imgs = max_images if max_images is not None else _IMPORT_MAX_IMAGES
    _max_b = max_bytes if max_bytes is not None else _IMPORT_MAX_BYTES

    # Find article rows and their image URLs.
    article_row_indices = [
        i for i, rr in enumerate(report.resolved) if rr.content_type == "article" and rr.blocks
    ]
    if not article_row_indices:
        return report

    # Collect unique URLs preserving first-seen order.
    url_to_refs: dict[str, list[tuple[int, int]]] = {}  # url → [(row_idx, block_idx)]
    for row_idx in article_row_indices:
        for block_idx, block in enumerate(report.resolved[row_idx].blocks or []):
            if isinstance(block, ImageBlock):
                url_to_refs.setdefault(block.url, []).append((row_idx, block_idx))

    unique_urls = list(url_to_refs.keys())
    if not unique_urls:
        return report

    # Image count ceiling — checked upfront before any I/O.
    if len(unique_urls) > _max_imgs:
        report.problems.append(
            RowProblem(
                0,
                "",
                f"план содержит {len(unique_urls)} уникальных изображений — "
                f"лимит {_max_imgs} (IMPORT_MAX_IMAGES). Сократите план.",
            )
        )
        _eject_article_rows(
            report, set(article_row_indices), "превышен лимит изображений (IMPORT_MAX_IMAGES)"
        )
        return report

    # Download all unique URLs with semaphore + pacing.
    url_data: dict[str, tuple[str, bytes]] = {}  # url → (filename, data)
    url_errors: dict[str, str] = {}  # url → error reason
    total_bytes = 0
    bytes_ceiling_hit = False
    sem = asyncio.Semaphore(_max_conc)

    async def _download(url: str) -> None:
        nonlocal total_bytes, bytes_ceiling_hit
        if bytes_ceiling_hit:
            url_errors[url] = "превышен лимит суммарного объёма (IMPORT_MAX_BYTES)"
            return
        async with sem:
            await _plan_sleep(_pacing)
            try:
                media = await fetch_media(url, client=client, max_bytes=_IMPORT_IMAGE_MAX_BYTES)
            except MediaSourceError as exc:
                url_errors[url] = str(exc)
                return
            if total_bytes + len(media.data) > _max_b:
                bytes_ceiling_hit = True
                url_errors[url] = "превышен лимит суммарного объёма (IMPORT_MAX_BYTES)"
                return
            total_bytes += len(media.data)
            url_data[url] = (media.filename, media.data)

    await asyncio.gather(*[_download(url) for url in unique_urls])

    if bytes_ceiling_hit:
        report.problems.append(
            RowProblem(
                0,
                "",
                f"суммарный объём изображений превысил лимит "
                f"{_max_b // (1024 * 1024)} МБ (IMPORT_MAX_BYTES).",
            )
        )
        _eject_article_rows(
            report, set(article_row_indices), "превышен лимит суммарного объёма байт"
        )
        return report

    # Save all successful downloads at once (single save_binaries call → unique indices).
    ordered_success = [u for u in unique_urls if u in url_data]
    if ordered_success:
        items = [url_data[u] for u in ordered_success]
        paths = save_binaries(items, import_key)
        url_paths: dict[str, str] = dict(zip(ordered_success, paths, strict=True))
    else:
        url_paths = {}

    # Update blocks and apply fail-closed policy.
    failed_indices: set[int] = set()
    for row_idx in article_row_indices:
        rr = report.resolved[row_idx]
        for block_idx, block in enumerate(rr.blocks or []):
            if not isinstance(block, ImageBlock):
                continue
            if block.url in url_paths:
                block.path = url_paths[block.url]
            else:
                reason = url_errors.get(block.url, "неизвестная ошибка загрузки")
                rr._fail_reason = f"блок {block_idx} ({block.url}): {reason}"  # type: ignore[attr-defined]
                failed_indices.add(row_idx)
                break  # stop at first failure in this row

    # Move failed rows from resolved to problems.
    new_resolved = []
    for i, rr in enumerate(report.resolved):
        if i in failed_indices:
            reason = getattr(rr, "_fail_reason", "ошибка загрузки изображения статьи")
            report.problems.append(RowProblem(rr.row_number, rr.file, f"статья: {reason}"))
        else:
            new_resolved.append(rr)
    report.resolved = new_resolved
    return report


def _eject_article_rows(report: ImportReport, article_idx: set[int], reason: str) -> None:
    """Move article rows (by index in report.resolved) to problems."""
    new_resolved = []
    for i, rr in enumerate(report.resolved):
        if i in article_idx:
            report.problems.append(RowProblem(rr.row_number, rr.file, reason))
        else:
            new_resolved.append(rr)
    report.resolved = new_resolved
