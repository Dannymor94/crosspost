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

# Дефолтный текст ссылки (фраза перед URL / гиперссылка): пользователь может переопределить.
DEFAULT_LINK_PHRASE = "Подробнее"

# Шаблон подстановки: {phrase} становится кликабельным HTML-тегом <a href>.
# Telegram-адаптер уже использует parse_mode="html" → тег рендерится как гиперссылка.
LINK_TEMPLATE = '{text}\n\n<a href="{url}">{phrase}</a>'

# Причина fail-closed: статья не создана (исчерпаны ретраи) → пост не публикуется.
NO_ARTICLE_REASON = "статья Telegraph не создана — пост не опубликован"


def render_with_link(
    text: str,
    url: str,
    template: str = LINK_TEMPLATE,
    link_phrase: str = DEFAULT_LINK_PHRASE,
) -> str:
    """Подставить текст поста, URL статьи и фразу ссылки в шаблон.

    При пустом `text` не оставляем ведущих переносов/пробелов.
    `link_phrase` — текст, отображаемый вместо сырого URL (например «Подробнее»).
    """
    rendered = template.format(text=text, url=url, phrase=link_phrase or DEFAULT_LINK_PHRASE)
    return rendered.lstrip("\n ") if not text else rendered


def article_content(text: str, title: str | None) -> CanonicalContent:
    """Синтез CanonicalContent(ARTICLE) из тела лонгрида и заголовка.

    Отдельная сущность от поста: пост остаётся POST (для TG), статья — ARTICLE (для
    Telegraph). Их не сливать (CLAUDE.md §две сущности контента по type).
    """
    return CanonicalContent(type=ContentType.ARTICLE, title=title, text=text)
