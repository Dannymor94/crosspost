"""Тесты Telegraph-адаптера. Мок площадки — httpx.AsyncClient.

API: https://telegra.ph/api
  createPage: POST https://api.telegra.ph/createPage
    Параметры: access_token (req), title (req), content (req, JSON)
    Ответ: {ok: bool, result: {url, path, …}}
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crosspost.adapters.api.telegraph import TelegraphAdapter, _text_to_nodes
from crosspost.adapters.base import ResultStatus
from crosspost.content.canonical import CanonicalContent, ContentType

_TOKEN = "test-telegraph-token"
_PAGE_URL = "https://telegra.ph/Test-Article-01-01"
_PATCH_TARGET = "crosspost.adapters.api.telegraph.httpx.AsyncClient"


# ── фикстуры ────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_article() -> CanonicalContent:
    return CanonicalContent(
        type=ContentType.ARTICLE,
        title="Тестовая статья",
        text="Первый параграф.\n\nВторой параграф.",
    )


def _make_client(response_json: dict) -> MagicMock:
    """Мок httpx.AsyncClient как async context manager."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = response_json

    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.fixture
def mock_http_ok():
    """createPage → ok=True, url получен."""
    client = _make_client({"ok": True, "result": {"url": _PAGE_URL, "path": "Test-Article-01-01"}})
    with patch(_PATCH_TARGET, return_value=client):
        yield client


@pytest.fixture
def mock_http_error():
    """createPage → ok=False, ACCESS_TOKEN_INVALID."""
    client = _make_client({"ok": False, "error": "ACCESS_TOKEN_INVALID"})
    with patch(_PATCH_TARGET, return_value=client):
        yield client


# ── тесты адаптера ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_when_already_published(store, publication_id, sample_article, mock_http_ok):
    """Идемпотентность: (publication_id, channel) уже done → SKIPPED без HTTP-запроса."""
    store.mark_done(publication_id, "telegraph", external_id=_PAGE_URL)
    adapter = TelegraphAdapter(_TOKEN, store)

    result = await adapter.publish(sample_article, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    mock_http_ok.post.assert_not_called()


@pytest.mark.asyncio
async def test_publishes_article_and_returns_url(
    store, publication_id, sample_article, mock_http_ok
):
    """Happy path: статья опубликована, external_id = URL страницы Telegraph."""
    adapter = TelegraphAdapter(_TOKEN, store)

    result = await adapter.publish(sample_article, publication_id=publication_id)

    assert result.status is ResultStatus.DONE
    assert result.external_id == _PAGE_URL
    assert store.is_done(publication_id, "telegraph")

    call = mock_http_ok.post.call_args
    assert "api.telegra.ph/createPage" in call.args[0]
    data = call.kwargs["data"]
    assert data["access_token"] == _TOKEN
    assert data["title"] == "Тестовая статья"
    assert "content" in data


@pytest.mark.asyncio
@pytest.mark.parametrize("ctype", [ContentType.POST, ContentType.REEL, ContentType.STORY])
async def test_skips_unsupported_content_types(store, publication_id, ctype, mock_http_ok):
    """post / reel / story → SKIPPED без HTTP-запроса.

    Capability-матрица отсекает их раньше, но адаптер защищается сам.
    """
    content = CanonicalContent(type=ctype, text="Текст поста")
    adapter = TelegraphAdapter(_TOKEN, store)

    result = await adapter.publish(content, publication_id=publication_id)

    assert result.status is ResultStatus.SKIPPED
    mock_http_ok.post.assert_not_called()


@pytest.mark.asyncio
async def test_returns_failed_on_telegraph_api_error(
    store, publication_id, sample_article, mock_http_error
):
    """Telegraph возвращает ok=False → FAILED, ошибка из поля error."""
    adapter = TelegraphAdapter(_TOKEN, store)

    result = await adapter.publish(sample_article, publication_id=publication_id)

    assert result.status is ResultStatus.FAILED
    assert result.error == "ACCESS_TOKEN_INVALID"
    assert not store.is_done(publication_id, "telegraph")


@pytest.mark.asyncio
async def test_uses_content_title_as_page_title(store, publication_id, mock_http_ok):
    """content.title → заголовок страницы Telegraph."""
    content = CanonicalContent(
        type=ContentType.ARTICLE,
        title="Мой заголовок",
        text="Тело.",
    )
    adapter = TelegraphAdapter(_TOKEN, store)
    await adapter.publish(content, publication_id=publication_id)

    data = mock_http_ok.post.call_args.kwargs["data"]
    assert data["title"] == "Мой заголовок"


@pytest.mark.asyncio
async def test_falls_back_to_text_when_no_title(store, publication_id, mock_http_ok):
    """Нет content.title → первые 256 символов text как заголовок."""
    content = CanonicalContent(
        type=ContentType.ARTICLE,
        title=None,
        text="Текст без явного заголовка.",
    )
    adapter = TelegraphAdapter(_TOKEN, store)
    await adapter.publish(content, publication_id=publication_id)

    data = mock_http_ok.post.call_args.kwargs["data"]
    assert data["title"] == "Текст без явного заголовка."


# ── тесты рендера ────────────────────────────────────────────────────────────


def test_text_to_nodes_splits_paragraphs_on_double_newline():
    """Двойной перенос строки → два параграфа <p>."""
    nodes = _text_to_nodes("Абзац 1.\n\nАбзац 2.")
    assert len(nodes) == 2
    assert nodes[0]["tag"] == "p"
    assert nodes[1]["tag"] == "p"
    assert "Абзац 1." in nodes[0]["children"]
    assert "Абзац 2." in nodes[1]["children"]


def test_text_to_nodes_single_paragraph():
    """Одиночная строка → один параграф <p>."""
    nodes = _text_to_nodes("Одна строка.")
    assert nodes == [{"tag": "p", "children": ["Одна строка."]}]


def test_text_to_nodes_inline_newline_becomes_br():
    """Одиночный перенос внутри параграфа → <br> между строками."""
    nodes = _text_to_nodes("Строка 1\nСтрока 2")
    assert len(nodes) == 1
    children = nodes[0]["children"]
    assert "Строка 1" in children
    assert {"tag": "br"} in children
    assert "Строка 2" in children


def test_text_to_nodes_empty_string():
    """Пустой текст → один пустой параграф (не падает)."""
    nodes = _text_to_nodes("")
    assert len(nodes) == 1
    assert nodes[0]["tag"] == "p"
