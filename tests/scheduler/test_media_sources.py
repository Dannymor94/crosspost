"""Тесты Блока 3: гибкие источники медиа для импорта контент-плана.

Чистая логика (классификация значения, построение прямых ссылок, SSRF-гард по IP,
конфайнмент папки) тестируется на данных. Сетевой слой (fetch_media) — через httpx
MockTransport + подставной DNS-resolver, без реальной сети и без реальных приватных
адресов. Ссылки/секреты не логируем.
"""

from __future__ import annotations

import httpx
import pytest

from crosspost.scheduler.media_sources import (
    FetchedMedia,
    MediaSourceError,
    SourceKind,
    assert_public_http_url,
    classify_source,
    fetch_media,
    gdrive_direct_url,
    resolve_in_folder,
)

# ── classify_source (чистая) ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,kind",
    [
        ("01.jpg", SourceKind.FILENAME),
        ("2026/jan/01.jpg", SourceKind.FILENAME),
        ("https://example.com/pic.png", SourceKind.HTTP),
        ("http://example.com/pic.png", SourceKind.HTTP),
        ("https://drive.google.com/file/d/ABC123/view", SourceKind.GDRIVE),
        ("https://disk.yandex.ru/i/abcDEF123", SourceKind.YADISK),
        ("https://yadi.sk/i/abcDEF123", SourceKind.YADISK),
    ],
)
def test_classify_source(value, kind):
    assert classify_source(value) == kind


# ── gdrive_direct_url (чистая) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://drive.google.com/file/d/FILEID123/view?usp=sharing",
        "https://drive.google.com/open?id=FILEID123",
        "https://drive.google.com/uc?id=FILEID123&export=download",
    ],
)
def test_gdrive_direct_url_extracts_id(url):
    direct = gdrive_direct_url(url)
    assert "id=FILEID123" in direct
    assert "export=download" in direct


def test_gdrive_direct_url_rejects_without_id():
    with pytest.raises(MediaSourceError):
        gdrive_direct_url("https://drive.google.com/drive/folders/xxx")


# ── SSRF-гард (чистая, с подставным resolver) ─────────────────────────────────


def _resolve_to(ip: str):
    def _r(host: str) -> list[str]:
        return [ip]

    return _r


def test_assert_public_http_url_rejects_non_http():
    with pytest.raises(MediaSourceError):
        assert_public_http_url("ftp://example.com/x", resolve=_resolve_to("8.8.8.8"))
    with pytest.raises(MediaSourceError):
        assert_public_http_url("file:///etc/passwd", resolve=_resolve_to("8.8.8.8"))


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.1.2.3", "192.168.0.5", "169.254.1.1", "172.16.0.1", "::1"],
)
def test_assert_public_http_url_blocks_private(ip):
    with pytest.raises(MediaSourceError):
        assert_public_http_url("https://internal.example/x", resolve=_resolve_to(ip))


def test_assert_public_http_url_allows_global():
    # Не должно бросать.
    assert_public_http_url("https://example.com/pic.png", resolve=_resolve_to("93.184.216.34"))


# ── resolve_in_folder (чистая, на файловой системе) ───────────────────────────


def test_resolve_in_folder_finds_file(tmp_path):
    (tmp_path / "sub").mkdir()
    f = tmp_path / "sub" / "01.jpg"
    f.write_bytes(b"img")
    got = resolve_in_folder(str(tmp_path), "sub/01.jpg")
    assert got == f


def test_resolve_in_folder_rejects_escape(tmp_path):
    (tmp_path / "secret.txt").write_bytes(b"x")
    with pytest.raises(MediaSourceError):
        resolve_in_folder(str(tmp_path / "root"), "../secret.txt")


def test_resolve_in_folder_rejects_absolute(tmp_path):
    with pytest.raises(MediaSourceError):
        resolve_in_folder(str(tmp_path), "/etc/passwd")


def test_resolve_in_folder_missing_file(tmp_path):
    with pytest.raises(MediaSourceError):
        resolve_in_folder(str(tmp_path), "nope.jpg")


# ── fetch_media (I/O через мок) ───────────────────────────────────────────────

_IMG = b"\xff\xd8\xff\xe0img-bytes"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_media_from_folder(tmp_path):
    f = tmp_path / "01.jpg"
    f.write_bytes(_IMG)
    result = await fetch_media("01.jpg", folder=str(tmp_path))
    assert isinstance(result, FetchedMedia)
    assert result.data == _IMG
    assert result.filename == "01.jpg"
    assert result.kind == SourceKind.FILENAME


async def test_fetch_media_from_folder_missing_is_error(tmp_path):
    with pytest.raises(MediaSourceError):
        await fetch_media("missing.jpg", folder=str(tmp_path))


async def test_fetch_media_http_download():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_IMG, headers={"content-type": "image/jpeg"})

    async with _client(handler) as client:
        result = await fetch_media(
            "https://example.com/pics/photo.jpg",
            folder="",
            client=client,
            resolve=_resolve_to("93.184.216.34"),
        )
    assert result.data == _IMG
    assert result.filename == "photo.jpg"
    assert result.kind == SourceKind.HTTP


async def test_fetch_media_http_blocks_private():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_IMG)

    async with _client(handler) as client:
        with pytest.raises(MediaSourceError):
            await fetch_media(
                "https://internal/pic.jpg",
                folder="",
                client=client,
                resolve=_resolve_to("127.0.0.1"),
            )


async def test_fetch_media_too_large():
    big = b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "image/jpeg"})

    async with _client(handler) as client:
        with pytest.raises(MediaSourceError):
            await fetch_media(
                "https://example.com/big.jpg",
                folder="",
                client=client,
                max_bytes=1000,
                resolve=_resolve_to("93.184.216.34"),
            )


async def test_fetch_media_gdrive():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "export=download" in str(request.url)
        assert "id=FILEID123" in str(request.url)
        return httpx.Response(200, content=_IMG, headers={"content-type": "image/jpeg"})

    async with _client(handler) as client:
        result = await fetch_media(
            "https://drive.google.com/file/d/FILEID123/view",
            folder="",
            client=client,
            resolve=_resolve_to("142.250.1.1"),
        )
    assert result.data == _IMG
    assert result.kind == SourceKind.GDRIVE


async def test_fetch_media_yadisk():
    href = "https://downloader.disk.yandex.ru/disk/real-download/abc.jpg"

    def handler(request: httpx.Request) -> httpx.Response:
        if "cloud-api.yandex.net" in str(request.url):
            return httpx.Response(
                200, json={"href": href}, headers={"content-type": "application/json"}
            )
        assert str(request.url) == href
        return httpx.Response(200, content=_IMG, headers={"content-type": "image/jpeg"})

    async with _client(handler) as client:
        result = await fetch_media(
            "https://disk.yandex.ru/i/abcDEF123",
            folder="",
            client=client,
            resolve=_resolve_to("77.88.55.55"),
        )
    assert result.data == _IMG
    assert result.kind == SourceKind.YADISK
