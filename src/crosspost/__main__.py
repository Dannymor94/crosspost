"""CLI кросс-постинга — точка входа MVP-0.

    python -m crosspost post --type post --text "..." --image a.jpg --to telegram,vk
    python -m crosspost login --channel yandex

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
from crosspost.adapters.browser.vk_wall import VKWallBrowserAdapter
from crosspost.adapters.browser.yandex import YandexBrowserAdapter
from crosspost.config import load_config, parse_bool
from crosspost.content.canonical import CanonicalContent, ContentType
from crosspost.content.capabilities import supports
from crosspost.content.validation import validate
from crosspost.orchestrator.task import JSONIdempotencyStore, new_publication_id

# Браузерный тир (post-MVP) — в API-фабрику не пускаем (граница API↔браузер).
_BROWSER_CHANNELS = {"whatsapp", "instagram", "dzen"}  # yandex реализован отдельной веткой

# Каналы, поддерживающие ручной логин через браузер.
_LOGIN_SUPPORTED = {"yandex", "vk", "vk_wall"}

# Точка входа для ручного логина: открываем страницу канала, она сама редиректнет на паспорт.
_LOGIN_ENTRY_URLS: dict[str, str] = {
    "yandex": "https://yandex.ru/sprav/",
    "vk": "https://vk.com/",
    "vk_wall": "https://vk.com/",
}

# Маркеры «ещё не вошёл» в URL — если после Enter они остались, вход не завершён.
_LOGIN_REJECT_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "yandex": ("passport.yandex", "auth/login"),
    "vk": ("vk.com/login", "id.vk.com"),
    "vk_wall": ("vk.com/login", "id.vk.com"),
}


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
        headless = parse_bool(cfg.get("BROWSER_HEADLESS", "false"))
        group_id = abs(int(cfg["VK_GROUP_ID"]))
        return VKBrowserAdapter(group_id, store, headless=headless)

    if channel == "vk_api":
        # API-тир ВК — оставлен на случай появления рабочего user-токена.
        from vkbottle import API
        api = API(token=cfg["VK_ACCESS_TOKEN"])
        photo_upload = parse_bool(cfg.get("VK_PHOTO_UPLOAD_ENABLED", "true"))
        return VKAdapter(api, target=cfg["VK_GROUP_ID"], store=store, photo_upload=photo_upload)

    if channel == "yandex":
        headless = parse_bool(cfg.get("BROWSER_HEADLESS", "false"))
        org_id = cfg["YANDEX_ORG_ID"]
        return YandexBrowserAdapter(org_id, store, headless=headless)

    if channel == "vk_wall":
        headless = parse_bool(cfg.get("BROWSER_HEADLESS", "false"))
        screen_name = cfg.get("VK_GROUP_SCREEN_NAME", cfg.get("VK_GROUP_URL", "medithou"))
        return VKWallBrowserAdapter(screen_name, store, headless=headless)

    if channel in _BROWSER_CHANNELS:
        raise ValueError(
            f"{channel}: браузерный канал пока не реализован (реализованы: vk, yandex, vk_wall)"
        )
    raise ValueError(
        f"build_adapter: неизвестный канал {channel!r} (ожидались: telegram, vk, yandex, vk_api, vk_wall)"
    )


async def _run_login(channel: str, cfg: dict, *, _input=None) -> int:
    """Открыть браузер для ручного логина в канал.

    Держит браузер открытым, пока пользователь не подтвердит вход (Enter).
    ПОСЛЕ подтверждения экспортирует storageState (куки + localStorage) в файл —
    именно этот файл, а не persistent-профиль, publish использует как сессию.
    """
    from crosspost.adapters.browser.base_browser import (
        is_logged_in,
        login_context,
        storage_state_path,
    )

    if channel not in _LOGIN_SUPPORTED:
        print(f"login: канал {channel!r} не поддерживает браузерный логин")
        print(f"Доступно: {', '.join(sorted(_LOGIN_SUPPORTED))}")
        return 1

    url = _LOGIN_ENTRY_URLS[channel]
    reject = _LOGIN_REJECT_FRAGMENTS.get(channel, ())
    wait = _input or input

    async def _prompt(msg: str) -> None:
        print(msg)
        await asyncio.get_event_loop().run_in_executor(None, wait)

    print(f"Открываю браузер для {channel} ({url}) ...")
    async with login_context(channel, headless=False) as (page, save_state):
        await page.goto(url, wait_until="domcontentloaded")
        print("\nВойдите в аккаунт в открывшемся браузере.")
        print("ВАЖНО: не закрывайте браузер вручную. Дождитесь РАБОЧЕГО кабинета,")
        await _prompt("затем вернитесь сюда и нажмите Enter. →")

        # Подтверждение: пока URL выглядит как страница логина — не сохраняем,
        # держим браузер открытым и просим войти ещё раз.
        while not await is_logged_in(page, reject_url_fragments=reject):
            print("\n⚠ Похоже, вход ещё не завершён (открыта страница логина).")
            await _prompt("Завершите вход, дождитесь кабинета и нажмите Enter ещё раз. →")

        # Вошли — экспортируем сессию ДО закрытия контекста.
        state_file = await save_state()

    print(f"\n✓ Сессия {channel} сохранена: {state_file}")
    print("Теперь `crosspost post --to " + channel + "` пойдёт без повторного логина.")
    return 0


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

    lg = sub.add_parser("login", help="ручной логин в браузерный канал")
    lg.add_argument("--channel", required=True, help=f"канал: {', '.join(sorted(_LOGIN_SUPPORTED))}")

    args = parser.parse_args(argv)

    if args.cmd == "login":
        cfg = load_config()
        return asyncio.run(_run_login(args.channel, cfg))

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
