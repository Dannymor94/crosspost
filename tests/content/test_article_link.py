"""Тесты БЛОКА 0: подстановка ссылки на статью + синтез CanonicalContent(ARTICLE)."""

from __future__ import annotations

from crosspost.content.article_link import (
    DEFAULT_LINK_PHRASE,
    LINK_TEMPLATE,
    NO_ARTICLE_REASON,
    article_content,
    render_with_link,
)
from crosspost.content.canonical import ContentType


def test_default_template_substitutes_text_and_url():
    out = render_with_link("Короткий текст", "https://telegra.ph/Test-08-12")
    assert "Короткий текст" in out
    assert "https://telegra.ph/Test-08-12" in out
    assert DEFAULT_LINK_PHRASE in out


def test_default_template_shape():
    assert "{text}" in LINK_TEMPLATE
    assert "{url}" in LINK_TEMPLATE
    assert "{phrase}" in LINK_TEMPLATE


def test_empty_text_no_leading_newlines():
    out = render_with_link("", "https://telegra.ph/Test-08-12")
    assert not out.startswith("\n")
    assert "https://telegra.ph/Test-08-12" in out
    assert DEFAULT_LINK_PHRASE in out


def test_custom_link_phrase():
    out = render_with_link("текст", "https://telegra.ph/T", link_phrase="Читать")
    assert "Читать" in out
    assert "https://telegra.ph/T" in out


def test_custom_template():
    out = render_with_link("t", "u", template="{text} :: {url} ({phrase})", link_phrase="P")
    assert out == "t :: u (P)"


def test_bare_url_not_escaped():
    url = "https://telegra.ph/Zagolovok-s-Defisom-08-12"
    out = render_with_link("текст", url)
    assert url in out  # голый URL — Telegram сам развернёт Instant View


def test_article_content_is_article_type():
    c = article_content("тело лонгрида", "Заголовок")
    assert c.type is ContentType.ARTICLE
    assert c.title == "Заголовок"
    assert c.text == "тело лонгрида"


def test_article_content_title_optional():
    c = article_content("тело", None)
    assert c.type is ContentType.ARTICLE
    assert c.title is None


def test_no_article_reason_is_nonempty():
    assert NO_ARTICLE_REASON
    assert isinstance(NO_ARTICLE_REASON, str)
