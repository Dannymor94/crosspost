"""Telegraph-адаптер. API-тир.

Публикует статью (ContentType.ARTICLE) через HTTP REST API Telegraph.
post / reel / story → SKIPPED: не поддерживаются платформой.
Идемпотентность по внутреннему publication_id.

API: https://telegra.ph/api
  Используемые методы:
    createPage  — POST https://api.telegra.ph/createPage
"""

from __future__ import annotations

import json

import httpx

from crosspost.adapters.base import ChannelAdapter, ChannelResult, ResultStatus
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.orchestrator.task import IdempotencyStore

_API_BASE = "https://api.telegra.ph"


def _text_to_nodes(text: str) -> list[dict]:
    """Текст статьи → массив Telegraph Node.

    Параграфы разделяются двойным переносом строки (\\n\\n).
    Одиночный перенос внутри параграфа → тег <br>.

    Spec: https://telegra.ph/api#NodeElement
    Поддерживаемые теги: p, br, b, i, a, code, pre, h3, h4, …
    """
    nodes: list[dict] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        children: list = []
        for i, line in enumerate(block.split("\n")):
            if i > 0:
                children.append({"tag": "br"})
            if line:
                children.append(line)
        nodes.append({"tag": "p", "children": children or [""]})
    return nodes or [{"tag": "p", "children": [text or ""]}]


class TelegraphAdapter:
    channel = "telegraph"

    def __init__(self, access_token: str, store: IdempotencyStore) -> None:
        self._token = access_token
        self._store = store

    async def publish(
        self,
        content: CanonicalContent,
        *,
        publication_id: str,
    ) -> ChannelResult:
        # 1) идемпотентность — дедуп по внутреннему ключу, не по external_id
        if self._store.is_done(publication_id, self.channel):
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        # 2) Telegraph поддерживает только статьи
        if content.type != ContentType.ARTICLE:
            return ChannelResult(self.channel, ResultStatus.SKIPPED)

        # 3) рендер: title обязателен для createPage (1-256 символов)
        title = (content.title or content.text[:256] or "Untitled").strip()[:256]
        nodes = _text_to_nodes(content.text)

        # 4) POST createPage — https://telegra.ph/api#createPage
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_API_BASE}/createPage",
                data={
                    "access_token": self._token,
                    "title": title,
                    "content": json.dumps(nodes, ensure_ascii=False),
                },
            )
        resp.raise_for_status()
        payload = resp.json()

        if not payload.get("ok"):
            error = payload.get("error", "unknown Telegraph error")
            return ChannelResult(self.channel, ResultStatus.FAILED, error=error)

        # 5) квитанция — полный URL страницы (для верификации / правки / удаления)
        external_id = payload["result"]["url"]
        self._store.mark_done(publication_id, self.channel, external_id=external_id)
        return ChannelResult(self.channel, ResultStatus.DONE, external_id=external_id)


# проверка соответствия контракту на этапе импорта/тайпчека
_: type[ChannelAdapter] = TelegraphAdapter  # noqa: E305
