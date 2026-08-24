"""Гибкие источники медиа для импорта контент-плана. Блок 3.

Значение колонки «файл» может быть:
  а) имя файла         → ищется в ПАПКЕ-ИСТОЧНИКЕ (путь задаётся в настройках);
  б) прямая http(s)-ссылка на изображение → скачать;
  в) публичная ссылка Яндекс.Диска → получить href через их API, скачать.

Google Drive как источник изображения не поддерживается и отклоняется явно.

Тип определяется автоматически по виду значения (classify_source), без ручного
выбора. Скачанное кладётся в media_store как обычные загруженные файлы.

РАЗДЕЛЕНИЕ: чистая логика (классификация, SSRF-гард по
IP, конфайнмент папки) — без сети и БД, тестируется на данных. Сеть (fetch_media)
— отдельный слой, мокается через httpx MockTransport + подставной DNS-resolver.

Безопасность (решения пользователя):
  • SSRF-гард: только http/https; хост резолвится, каждый IP проверяется — приватные
    /loopback/link-local (10/8, 127/8, 169.254/16, 192.168/16, ::1, …) запрещены;
  • лимит размера (max_bytes) и таймаут скачивания;
  • папка-источник: подпапки внутри корня разрешены, `../`-побеги и абсолютные
    пути — запрещены.
Ссылки/секреты НЕ логируем.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

_TIMEOUT_SECONDS = 20.0
_MAX_BYTES = 25 * 1024 * 1024  # 25 МБ — потолок картинки по умолчанию
_YADISK_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
class MediaSourceError(ValueError):
    """Источник медиа непригоден: не скачать, приватный адрес, нет файла, велик размер."""


class SourceKind(StrEnum):
    FILENAME = "filename"  # имя файла в папке-источнике
    HTTP = "http"  # прямая http(s)-ссылка на изображение
    GDRIVE = "gdrive"  # ссылка Google Drive
    YADISK = "yadisk"  # публичная ссылка Яндекс.Диска


@dataclass(frozen=True)
class FetchedMedia:
    """Скачанный/прочитанный файл + откуда он взят (для отчёта связывания)."""

    data: bytes
    filename: str
    kind: SourceKind


# ── Классификация значения (чистая) ───────────────────────────────────────────


def classify_source(value: str) -> SourceKind:
    """Вид значения колонки «файл» → тип источника. Без сети."""
    v = (value or "").strip()
    low = v.lower()
    if low.startswith(("http://", "https://")):
        host = urlparse(v).hostname or ""
        if "drive.google.com" in host or "docs.google.com" in host:
            return SourceKind.GDRIVE
        if "disk.yandex" in host or "yadi.sk" in host:
            return SourceKind.YADISK
        return SourceKind.HTTP
    return SourceKind.FILENAME


# ── SSRF-гард (чистая логика + инъектируемый resolver) ─────────────────────────


def _default_resolve(host: str) -> list[str]:
    """DNS-резолв хоста → список IP-строк (I/O; в тестах подменяется)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise MediaSourceError("Не удалось разрешить адрес ссылки.") from exc
    return [str(info[4][0]) for info in infos]


def _is_global_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    )


def assert_public_http_url(url: str, *, resolve: Callable[[str], list[str]] | None = None) -> None:
    """Пропустить только публичный http/https-URL. Иначе — MediaSourceError.

    Схема ограничена http/https; хост резолвится, каждый полученный IP обязан быть
    глобальным (не приватный/loopback/link-local). Защита от SSRF.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise MediaSourceError("Разрешены только ссылки http/https.")
    host = parsed.hostname
    if not host:
        raise MediaSourceError("Некорректная ссылка.")
    resolver = resolve or _default_resolve
    ips = resolver(host)
    if not ips:
        raise MediaSourceError("Не удалось разрешить адрес ссылки.")
    for ip in ips:
        if not _is_global_ip(ip):
            raise MediaSourceError("Ссылка ведёт на приватный/локальный адрес.")


# ── Папка-источник (чистая, на файловой системе) ──────────────────────────────


def resolve_in_folder(folder: str, name: str) -> Path:
    """Имя файла (возможно с подпапкой) → путь ВНУТРИ корня folder.

    Разрешены подпапки внутри корня; `../`-побеги и абсолютные пути запрещены.
    Файл обязан существовать.
    """
    if not folder:
        raise MediaSourceError("Не задана папка-источник картинок.")
    if Path(name).is_absolute() or name.startswith(("/", "\\")):
        raise MediaSourceError("Абсолютный путь к файлу запрещён.")
    root = Path(folder).resolve()
    candidate = (root / name).resolve()
    if root != candidate and root not in candidate.parents:
        raise MediaSourceError("Путь выходит за пределы папки-источника.")
    if not candidate.is_file():
        raise MediaSourceError(f"Файл «{name}» не найден в папке-источнике.")
    return candidate


# ── Скачивание (I/O; клиент и resolver инъектируемы для тестов) ────────────────


def _filename_from_url(url: str, fallback: str) -> str:
    path = urlparse(url).path
    base = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return base or fallback


async def _download(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    resolve: Callable[[str], list[str]] | None,
) -> bytes:
    """Скачать URL с проверкой SSRF и потолком размера. Стримом, без буфера целиком."""
    assert_public_http_url(url, resolve=resolve)
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        if resp.status_code != 200:
            raise MediaSourceError(f"Не удалось скачать файл (HTTP {resp.status_code}).")
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise MediaSourceError("Файл превышает допустимый размер.")
            chunks.append(chunk)
    if total == 0:
        raise MediaSourceError("Скачанный файл пуст.")
    return b"".join(chunks)


async def fetch_media(
    value: str,
    *,
    folder: str = "",
    client: httpx.AsyncClient | None = None,
    max_bytes: int = _MAX_BYTES,
    timeout: float = _TIMEOUT_SECONDS,
    resolve: Callable[[str], list[str]] | None = None,
) -> FetchedMedia:
    """Значение колонки «файл» → FetchedMedia (байты + имя + тип источника).

    Тип определяется classify_source. FILENAME читается из папки (без сети). URL —
    скачивается через client (создаётся свой, если не передан). YADISK сперва
    преобразуется в прямую ссылку. GDRIVE явно не поддерживается. Ошибки —
    MediaSourceError с человеческим текстом.
    """
    kind = classify_source(value)
    if kind is SourceKind.FILENAME:
        path = resolve_in_folder(folder, value.strip())
        return FetchedMedia(path.read_bytes(), path.name, kind)

    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        if kind is SourceKind.GDRIVE:
            raise MediaSourceError(
                "Google Drive как источник изображения не поддерживается. "
                "Используйте Яндекс.Диск или прямую http-ссылку на изображение."
            )
        if kind is SourceKind.YADISK:
            href = await _yadisk_href(client, value, timeout=timeout, resolve=resolve)
            data = await _download(
                client, href, max_bytes=max_bytes, timeout=timeout, resolve=resolve
            )
            return FetchedMedia(data, _filename_from_url(href, "yadisk.jpg"), kind)
        # HTTP
        data = await _download(client, value, max_bytes=max_bytes, timeout=timeout, resolve=resolve)
        return FetchedMedia(data, _filename_from_url(value, "image.jpg"), kind)
    finally:
        if own_client:
            await client.aclose()


async def _yadisk_href(
    client: httpx.AsyncClient,
    public_url: str,
    *,
    timeout: float,
    resolve: Callable[[str], list[str]] | None,
) -> str:
    """Публичная ссылка Яндекс.Диска → прямая ссылка скачивания (API href)."""
    assert_public_http_url(_YADISK_API, resolve=resolve)
    resp = await client.get(_YADISK_API, params={"public_key": public_url}, timeout=timeout)
    if resp.status_code != 200:
        raise MediaSourceError(
            f"Яндекс.Диск: не удалось получить ссылку (HTTP {resp.status_code})."
        )
    try:
        href = resp.json().get("href")
    except (ValueError, TypeError) as exc:
        raise MediaSourceError("Яндекс.Диск: некорректный ответ API.") from exc
    if not href:
        raise MediaSourceError("Яндекс.Диск: ссылка недоступна.")
    return str(href)
