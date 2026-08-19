"""Импорт контент-плана из Google Таблицы по публичной ссылке. Милстоун «импорт».

БЕЗ OAuth: таблица с доступом «Все, у кого есть ссылка» отдаётся Google по
export-эндпоинту в CSV. Чистая логика (разбор ссылки, построение URL) отделена
от сетевого слоя (fetch_sheet_csv) — первую тестируем на данных, второй мокаем.

Проверено по докам Google:
  https://docs.google.com/spreadsheets/d/{ID}/export?format=csv&gid={GID}
Нет доступа → Google редиректит на accounts.google.com или отдаёт text/html
(страница входа) вместо text/csv — так распознаём «нет доступа по ссылке».

Ссылки НЕ логируем (могут содержать приватный id таблицы).
"""

from __future__ import annotations

import re

import httpx

_TIMEOUT_SECONDS = 20.0
_EXPORT_TMPL = "https://docs.google.com/spreadsheets/d/{sid}/export?format=csv"

# id таблицы в ссылке вида /spreadsheets/d/<ID>/...
_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
# gid вкладки: ?gid=123 или #gid=123
_GID_RE = re.compile(r"[?#&]gid=(\d+)")


class RemoteTableError(ValueError):
    """Таблицу нельзя получить по ссылке (не таблица / нет доступа / сеть / пусто)."""


def extract_sheet_id(url: str) -> tuple[str, str | None]:
    """Ссылка на Google Таблицу → (sheet_id, gid|None). Иначе RemoteTableError.

    Понимает формы /edit#gid=, ?usp=sharing, голый /d/<id>. gid — вкладка (опц.).
    """
    m = _SHEET_ID_RE.search(url or "")
    if not m:
        raise RemoteTableError(
            "Ссылка не похожа на Google Таблицу (ожидается docs.google.com/spreadsheets/d/…)."
        )
    gid_match = _GID_RE.search(url)
    return m.group(1), (gid_match.group(1) if gid_match else None)


def csv_export_url(sheet_id: str, gid: str | None) -> str:
    """(sheet_id, gid) → URL экспорта листа в CSV."""
    url = _EXPORT_TMPL.format(sid=sheet_id)
    if gid:
        url += f"&gid={gid}"
    return url


async def fetch_sheet_csv(url: str, *, client: httpx.AsyncClient | None = None) -> bytes:
    """Скачать лист Google Таблицы как CSV-байты. Ошибки — человекочитаемые.

    Распознаёт «нет доступа по ссылке»: финальный ответ — HTML/страница входа
    (Google не даёт CSV анонимусу без публичного доступа).
    """
    sheet_id, gid = extract_sheet_id(url)
    export = csv_export_url(sheet_id, gid)

    owns = client is None
    cl = client or httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True)
    try:
        resp = await cl.get(export, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise RemoteTableError("Не удалось скачать таблицу — проверьте ссылку и сеть.") from exc
    finally:
        if owns:
            await cl.aclose()

    final_host = resp.url.host or ""
    content_type = resp.headers.get("content-type", "").lower()
    if "accounts.google.com" in final_host or content_type.startswith("text/html"):
        raise RemoteTableError(
            "Нет доступа к таблице. Откройте доступ по ссылке: "
            "Файл → Поделиться → Доступ «Все, у кого есть ссылка»."
        )
    if resp.status_code == 404:
        raise RemoteTableError("Таблица не найдена — проверьте ссылку.")
    if resp.status_code != 200:
        raise RemoteTableError(f"Google вернул ошибку {resp.status_code} при скачивании таблицы.")

    data = resp.content
    if not data.strip():
        raise RemoteTableError("Таблица пустая.")
    return data
