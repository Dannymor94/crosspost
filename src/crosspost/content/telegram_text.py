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


def split_headline(text_html: str) -> tuple[str, str]:
    """Разбить длинный пост на заголовок-подпись и тело.

    В двухсообщённом режиме фото не должно висеть немым: первая строка идёт
    подписью под фото, остальное — отдельным сообщением. Заголовок убирается из
    тела, чтобы не дублироваться.

    Возвращает (caption, body). Откат к ("", text) — если первой строки нет
    (нет перевода строки), она пустая, или не влезает в CAPTION_LIMIT.
    """
    head, sep, rest = text_html.partition("\n")
    if not sep or not head.strip() or visible_length(head) > CAPTION_LIMIT:
        return "", text_html
    return head, rest.lstrip("\n")


def exceeds_message_limit(text_html: str) -> bool:
    """Больше одного сообщения. Чанкинг не поддержан — вызывающий вернёт FAILED."""
    return visible_length(text_html) > MESSAGE_LIMIT


# ── Link parser ───────────────────────────────────────────────────────────────

_TG_LINK = re.compile(
    r"(?:https?://)?t\.me/"
    r"(?:"
    r"c/(?P<num_id>\d+)/(?P<num_topic>\d+)(?:/\d+)?"  # t.me/c/<num>/<topic>[/<sub>]
    r"|"
    r"(?P<username>[A-Za-z0-9_]{3,})/(?P<user_topic>\d+)(?:/\d+)?"  # t.me/<user>/<topic>[/<sub>]
    r")"
    r"$"
)


def parse_telegram_link(url: str) -> tuple[str, int | None]:
    """Parse a t.me link into (target_value, topic_id).

    Returns:
        (target_value, topic_id) — topic_id is None when the General topic (id=1).

    Raises:
        ValueError for unrecognised or non-t.me links.
    """
    url = url.strip()
    m = _TG_LINK.match(url)
    if not m:
        raise ValueError(f"Не удалось разобрать ссылку Telegram: {url!r}")

    if m.group("num_id"):
        target = f"-100{m.group('num_id')}"
        raw_topic = int(m.group("num_topic"))
    else:
        target = f"@{m.group('username')}"
        raw_topic = int(m.group("user_topic"))

    topic_id = None if raw_topic == 1 else raw_topic
    return target, topic_id
