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
from crosspost.adapters.api.vk import VKAdapter               # "vk_api" — ждёт рабочий токен
from crosspost.adapters.browser.vk import VKBrowserAdapter
from crosspost.adapters.browser.yandex import YandexBrowserAdapter
from crosspost.config import load_config, parse_bool
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.content.capabilities import supports
from crosspost.content.validation import validate
from crosspost.orchestrator.task import JSONIdempotencyStore, new_publication_id

# Браузерный тир (post-MVP) — в API-фабрику не пускаем (граница API↔браузер).
_BROWSER_CHANNELS = {"whatsapp", "instagram", "dzen"}  # yandex реализован отдельной веткой


async def build_adapter(channel: str, store):
    """Собрать адаптер канала из runtime/.env.

    "vk"     → VKBrowserAdapter (браузерный тир; API-путь заблокирован платформой).
    "vk_api" → VKAdapter (API-тир, ждёт рабочий токен — оставлен для будущего).
    Тяжёлые SDK импортируются ЛЕНИВО внутри ветки.
    """
    cfg = load_config()

    if channel == "telegram":
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        # userbot на StringSession: грузим сохранённую сессию из TG_SESSION_PATH,
        # стартуем (разовый интерактив телефон/код при первом запуске),
        # сохраняем строку обратно — повторные запуски кода не попросят.
        session_path = Path(cfg.get("TG_SESSION_PATH", "runtime/sessions/telegram.session"))
        session_str = session_path.read_text().strip() if session_path.exists() else None
        client = TelegramClient(StringSession(session_str), int(cfg["TG_API_ID"]), cfg["TG_API_HASH"])
        await client.start()
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(client.session.save())
        return TelegramAdapter(client, target=cfg["TG_TARGET_CHANNEL"], store=store)

    if channel == "vk":
        # Браузерный тир: API заблокирован (err 27/214), работаем через Playwright.
        profiles_dir = cfg.get("BROWSER_PROFILES_DIR", "runtime/browser_profiles")
        headless = parse_bool(cfg.get("BROWSER_HEADLESS", "false"))
        group_id = abs(int(cfg["VK_GROUP_ID"]))
        return VKBrowserAdapter(group_id, profiles_dir, store, headless=headless)

    if channel == "vk_api":
        # API-тир ВК — оставлен на случай появления рабочего user-токена.
        from vkbottle import API
        api = API(token=cfg["VK_ACCESS_TOKEN"])
        photo_upload = parse_bool(cfg.get("VK_PHOTO_UPLOAD_ENABLED", "true"))
        return VKAdapter(api, target=cfg["VK_GROUP_ID"], store=store, photo_upload=photo_upload)

    if channel == "yandex":
        profiles_dir = cfg.get("BROWSER_PROFILES_DIR", "runtime/browser_profiles")
        headless = parse_bool(cfg.get("BROWSER_HEADLESS", "false"))
        org_id = cfg["YANDEX_ORG_ID"]
        return YandexBrowserAdapter(org_id, profiles_dir, store, headless=headless)

    if channel in _BROWSER_CHANNELS:
        raise ValueError(
            f"{channel}: браузерный канал пока не реализован (реализованы: vk, yandex)"
        )
    raise ValueError(
        f"build_adapter: неизвестный канал {channel!r} (ожидались: telegram, vk, yandex, vk_api)"
    )


async def _run(content: CanonicalContent, channels: list[str], store) -> int:
    validate(content)
    publication_id = new_publication_id()
    failures = 0
    for ch in channels:
        if not supports(ch, content.type):
            print(f"✗ {ch}: тип {content.type.value} не поддерживается")
            failures += 1
            continue
        adapter = await build_adapter(ch, store)
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
