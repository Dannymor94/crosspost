"""Текстовые лимиты и разметка Telegram — вынесено из адаптера, чтобы он был тонким.

Telegram считает лимиты по ВИДИМОМУ тексту (после разбора HTML-разметки в entities),
а не по сырой строке с тегами. Поэтому длину меряем по видимому тексту, иначе
`<a href="…">` раздувает счёт и пост упирается в лимит там, где визуально ещё влезает.

Лимиты (не-Premium аккаунт; на Premium 2048 — свойство аккаунта, закладываться нельзя):
  CAPTION_LIMIT — подпись под медиа.
  MESSAGE_LIMIT — отдельное текстовое сообщение.
"""

from __future__ import annotations

import html as _html
import re

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

_TAG = re.compile(r"<[^>]+>")


def escape_html(text: str) -> str:
    """Экранировать видимый текст ПЕРЕД сборкой HTML.

    Амперсанд/угловые скобки в названии события иначе ломают разметку, и Telegram
    отклоняет сообщение целиком. Порядок важен: `&` первым, чтобы не переэкранировать
    уже вставленные сущности. Применять к КАЖДОМУ видимому фрагменту при сборке
    `<b>…</b>` / `<a href="…">…</a>`, не к готовой HTML-строке.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def visible_length(text_html: str) -> int:
    """Длина видимого текста: снять теги и раскодировать сущности.

    Это то, что Telegram засчитывает в лимит при parse_mode="html". Достаточно
    для гейта длины (реализовать полный HTML-парсер не нужно — набор тегов Telegram
    невелик, а `html.unescape` покрывает `&amp;`/`&lt;`/`&gt;` и числовые сущности).
    """
    return len(_html.unescape(_TAG.sub("", text_html)))


def caption_fits(text_html: str) -> bool:
    """Влезает ли текст в подпись под медиа (режим «фото + подпись»)."""
    return visible_length(text_html) <= CAPTION_LIMIT


def exceeds_message_limit(text_html: str) -> bool:
    """Больше одного сообщения. Чанкинг не поддержан — вызывающий вернёт FAILED."""
    return visible_length(text_html) > MESSAGE_LIMIT
