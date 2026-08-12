"""Связка «статья Telegraph → пост со ссылкой». Декларации + чистые хелперы.

Один элемент контент-плана порождает ДВА действия по порядку: сначала статья в
Telegraph (получаем URL), затем пост в канал (картинка + короткий текст + ссылка на
статью). Здесь живут только декларации формата и синтез контента статьи — без БД,
без адаптеров, без оркестрации (та — в orchestrator/article_flow.py).

Шаблон ссылки — ДЕКЛАРАЦИЯ (LINK_TEMPLATE), а не хардкод в адаптере: канал получает
уже готовый текст. Ссылка — голый https://telegra.ph/... , Telegram сам разворачивает
Instant View (экранирование не нужно). Per-profile override шаблона — позже.
"""

from __future__ import annotations

from crosspost.content.canonical import CanonicalContent, ContentType

# Дефолтный шаблон подстановки: короткий текст поста + строка со ссылкой на статью.
LINK_TEMPLATE = "{text}\n\nЧитать полностью → {url}"

# Причина fail-closed: статья не создана (исчерпаны ретраи) → пост не публикуется.
NO_ARTICLE_REASON = "статья Telegraph не создана — пост не опубликован"


def render_with_link(text: str, url: str, template: str = LINK_TEMPLATE) -> str:
    """Подставить текст поста и URL статьи в шаблон.

    При пустом `text` не оставляем ведущих переносов/пробелов (иначе пост начинался
    бы с пустых строк перед «Читать полностью →»).
    """
    rendered = template.format(text=text, url=url)
    return rendered.lstrip("\n ") if not text else rendered


def article_content(text: str, title: str | None) -> CanonicalContent:
    """Синтез CanonicalContent(ARTICLE) из тела лонгрида и заголовка.

    Отдельная сущность от поста: пост остаётся POST (для TG), статья — ARTICLE (для
    Telegraph). Их не сливать (CLAUDE.md §две сущности контента по type).
    """
    return CanonicalContent(type=ContentType.ARTICLE, title=title, text=text)
