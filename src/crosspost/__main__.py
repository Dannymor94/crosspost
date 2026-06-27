"""CLI кросс-постинга — точка входа MVP-0.

    python -m crosspost post --type post --text "..." --image a.jpg --to telegram,vk

Синхронно, состояние в JSON. Без очереди/веба/БД/браузера (см. MILESTONES.md).
Статус: КАРКАС. Сборка адаптеров и реальная отправка — задача фазы 0 (см. PLAN.md).
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from crosspost.adapters.api.telegram import TelegramAdapter
from crosspost.adapters.api.vk import VKAdapter
from crosspost.config import load_config
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.content.capabilities import supports
from crosspost.content.validation import validate
from crosspost.orchestrator.task import JSONIdempotencyStore, new_publication_id

# Браузерный тир (post-MVP) — в API-фабрику не пускаем (граница API↔браузер).
_BROWSER_CHANNELS = {"whatsapp", "instagram", "dzen", "yandex"}


def build_adapter(channel: str, store):
    """Собрать API-адаптер канала из runtime/.env.

    Единственная точка, знающая про конкретные клиенты. Тяжёлые SDK
    (telethon/vkbottle) импортируются ЛЕНИВО внутри ветки — не тащим в шапку,
    тесты остаются лёгкими. Браузерные каналы тут не появляются (post-MVP).
    """
    cfg = load_config()

    if channel == "telegram":
        from telethon import TelegramClient

        # userbot: сессия — файл (TG_SESSION_PATH); коннект/логин — при smoke (шаг 5).
        client = TelegramClient(
            cfg.get("TG_SESSION_PATH", "runtime/sessions/telegram.session"),
            int(cfg["TG_API_ID"]),
            cfg["TG_API_HASH"],
        )
        return TelegramAdapter(client, target=cfg["TG_TARGET_CHANNEL"], store=store)

    if channel == "vk":
        from vkbottle import API

        api = API(token=cfg["VK_ACCESS_TOKEN"])
        return VKAdapter(api, target=cfg["VK_GROUP_ID"], store=store)

    if channel in _BROWSER_CHANNELS:
        raise ValueError(
            f"{channel}: браузерный канал вне MVP-0 — здесь только API-тир (telegram, vk)"
        )
    raise ValueError(f"build_adapter: неизвестный канал {channel!r} (ожидались: telegram, vk)")


async def _run(content: CanonicalContent, channels: list[str], store) -> int:
    validate(content)
    publication_id = new_publication_id()
    failures = 0
    for ch in channels:
        if not supports(ch, content.type):
            print(f"✗ {ch}: тип {content.type.value} не поддерживается")
            failures += 1
            continue
        adapter = build_adapter(ch, store)
        result = await adapter.publish(content, publication_id=publication_id)
        mark = "✓" if result.error is None else "✗"
        print(f"{mark} {ch}: {result.status.value}"
              + (f" (id={result.external_id})" if result.external_id else "")
              + (f" — {result.error}" if result.error else ""))
        failures += int(result.error is not None)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crosspost")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post", help="опубликовать контент в каналы")
    p.add_argument("--type", required=True, choices=[t.value for t in ContentType])
    p.add_argument("--text", default="")
    p.add_argument("--title", default=None)
    p.add_argument("--image", action="append", default=[], help="путь к медиа (можно несколько)")
    p.add_argument("--to", required=True, help="каналы через запятую: telegram,vk")
    p.add_argument("--state", default="runtime/state.json")
    args = parser.parse_args(argv)

    content = CanonicalContent(
        type=ContentType(args.type),
        text=args.text,
        title=args.title,
        media_paths=[Path(x) for x in args.image],
    )
    channels = [c.strip() for c in args.to.split(",") if c.strip()]
    store = JSONIdempotencyStore(args.state)
    return asyncio.run(_run(content, channels, store))


if __name__ == "__main__":
    raise SystemExit(main())
