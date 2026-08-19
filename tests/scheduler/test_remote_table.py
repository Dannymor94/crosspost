"""Тесты Блока 1: импорт контент-плана из Google Таблицы по ссылке.

Чистая логика (разбор ссылки, построение export-URL) тестируется на данных.
Сетевой слой (fetch_sheet_csv) — через httpx MockTransport, без реальной сети.
Секреты/ссылки не логируем — здесь только проверяем поведение.
"""

from __future__ import annotations

import httpx
import pytest

from crosspost.scheduler.remote_table import (
    RemoteTableError,
    csv_export_url,
    extract_sheet_id,
    fetch_sheet_csv,
)

# ── extract_sheet_id (чистая) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected_id,expected_gid",
    [
        (
            "https://docs.google.com/spreadsheets/d/1AbC_dEF-123/edit#gid=456",
            "1AbC_dEF-123",
            "456",
        ),
        (
            "https://docs.google.com/spreadsheets/d/1AbC_dEF-123/edit?usp=sharing",
            "1AbC_dEF-123",
            None,
        ),
        (
            "https://docs.google.com/spreadsheets/d/1AbC_dEF-123",
            "1AbC_dEF-123",
            None,
        ),
        (
            "https://docs.google.com/spreadsheets/d/1AbC_dEF-123/edit?gid=789#gid=789",
            "1AbC_dEF-123",
            "789",
        ),
    ],
)
def test_extract_sheet_id_forms(url, expected_id, expected_gid):
    sheet_id, gid = extract_sheet_id(url)
    assert sheet_id == expected_id
    assert gid == expected_gid


def test_extract_sheet_id_rejects_non_sheet():
    with pytest.raises(RemoteTableError):
        extract_sheet_id("https://example.com/not-a-sheet")


def test_extract_sheet_id_rejects_drive_file():
    # Ссылка на файл Drive — не таблица.
    with pytest.raises(RemoteTableError):
        extract_sheet_id("https://drive.google.com/file/d/1AbC/view")


# ── csv_export_url (чистая) ───────────────────────────────────────────────────


def test_csv_export_url_with_gid():
    url = csv_export_url("SHEET1", "42")
    assert "spreadsheets/d/SHEET1/export" in url
    assert "format=csv" in url
    assert "gid=42" in url


def test_csv_export_url_without_gid():
    url = csv_export_url("SHEET1", None)
    assert "spreadsheets/d/SHEET1/export" in url
    assert "format=csv" in url
    assert "gid=" not in url


# ── fetch_sheet_csv (I/O через мок) ───────────────────────────────────────────

_CSV = b"file,text\n1.jpg,Hello\n"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_sheet_csv_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CSV, headers={"content-type": "text/csv"})

    async with _client(handler) as client:
        data = await fetch_sheet_csv(
            "https://docs.google.com/spreadsheets/d/SHEET1/edit#gid=0", client=client
        )
    assert data == _CSV


async def test_fetch_sheet_csv_no_access_html():
    # Нет доступа: Google отдаёт HTML-страницу входа вместо CSV.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>Sign in</html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    async with _client(handler) as client:
        with pytest.raises(RemoteTableError) as exc:
            await fetch_sheet_csv(
                "https://docs.google.com/spreadsheets/d/SHEET1/edit", client=client
            )
    assert "доступ" in str(exc.value).lower()


async def test_fetch_sheet_csv_redirect_to_login():
    # Нет доступа: редирект на accounts.google.com.
    def handler(request: httpx.Request) -> httpx.Response:
        if "accounts.google.com" in str(request.url):
            return httpx.Response(200, content=b"login", headers={"content-type": "text/html"})
        return httpx.Response(302, headers={"location": "https://accounts.google.com/ServiceLogin"})

    async with _client(handler) as client:
        with pytest.raises(RemoteTableError) as exc:
            await fetch_sheet_csv(
                "https://docs.google.com/spreadsheets/d/SHEET1/edit", client=client
            )
    assert "доступ" in str(exc.value).lower()


async def test_fetch_sheet_csv_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    async with _client(handler) as client:
        with pytest.raises(RemoteTableError):
            await fetch_sheet_csv(
                "https://docs.google.com/spreadsheets/d/SHEET1/edit", client=client
            )
