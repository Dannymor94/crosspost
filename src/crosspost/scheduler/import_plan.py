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

import calendar
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal

from crosspost.scheduler.media_sources import SourceKind, classify_source

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
        file_str = "" if file_val is None else str(file_val).strip()
        text_str = "" if text_val is None else str(text_val).strip()
        article_str = "" if article_val is None else str(article_val).strip()

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
        if not row.text:
            report.problems.append(RowProblem(row.row_number, row.file, "пустой текст поста"))
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
                report.resolved.append(
                    ResolvedRow(
                        row.row_number,
                        matched,
                        row.text,
                        rule.slot_datetime(ordinal),
                        source="rule",
                        article_text=row.article_text,
                        media_source=msource,
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
