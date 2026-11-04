#!/usr/bin/env python3
"""Одноразовая последовательная загрузка autoreels только в @systemyogi.

Проба: .venv/bin/python scripts/upload_reels.py --limit 1
После подтверждения: .venv/bin/python scripts/upload_reels.py --start 2
Прогресс печатается сразу; после ошибки продолжение задаётся вручную через --start.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo
from telethon.utils import get_peer_id

from crosspost.channels.validators import VALIDATORS, normalize_telegram_target
from crosspost.config import load_config
from crosspost.db.engine import get_db_url
from crosspost.db.models import ChannelTarget, Connection, ConnectionState
from crosspost.db.vault import decrypt_blob, get_vault

try:
    from crosspost.reels.manifest import _sha256, ffprobe
    from crosspost.reels.thumbnails import ensure_thumbnail
except ModuleNotFoundError:
    ensure_thumbnail = None

SOURCE = Path.home() / "Documents/autoreels/export/publish"
USERNAME = "@systemyogi"
CHAT_ID = -1002818632536


def collect_pairs(source: Path, expected_count: int = 69) -> list[tuple[int, Path, str]]:
    videos = {p.stem: p for p in source.iterdir() if p.is_file() and p.suffix == ".mp4"}
    texts = {p.stem: p for p in source.iterdir() if p.is_file() and p.suffix == ".txt"}
    if videos.keys() != texts.keys():
        raise RuntimeError(f"Неполные пары: {sorted(videos.keys() ^ texts.keys())}")
    rows = []
    for stem, video in videos.items():
        match = re.match(r"^(\d{3})_", stem)
        if not match:
            raise RuntimeError(f"Нет начального номера: {stem}")
        caption = texts[stem].read_bytes().decode("utf-8-sig", errors="strict").strip()
        if not caption or len(caption.encode("utf-16-le")) // 2 > 1024:
            raise RuntimeError(f"Пустая или слишком длинная подпись: {stem}")
        rows.append((int(match[1]), video, caption))
    rows.sort(key=lambda row: row[0])
    if [row[0] for row in rows] != list(range(1, expected_count + 1)):
        raise RuntimeError(
            f"Ожидались ровно {expected_count} пар с номерами 001–{expected_count:03}"
        )
    return rows


async def configured_target(profile: int) -> str:
    """Читаем только именованную цель нужного профиля; live-БД не меняем."""
    engine = create_async_engine(get_db_url())
    try:
        async with AsyncSession(engine) as session:
            rows = list(
                (
                    await session.scalars(
                        select(ChannelTarget).where(
                            ChannelTarget.profile_id == profile,
                            ChannelTarget.channel == "telegram",
                            ChannelTarget.name == "Основная",
                        )
                    )
                ).all()
            )
            vault = get_vault()
            matches = [
                row
                for row in rows
                if normalize_telegram_target(decrypt_blob(vault, row.value_blob)).casefold()
                in (USERNAME, str(CHAT_ID))
            ]
            if len(matches) != 1 or matches[0].topic_id is not None:
                raise RuntimeError(
                    "В конфигурации нет единственной цели Основная → @systemyogi без темы"
                )
            state = await session.scalar(
                select(Connection.state).where(
                    Connection.profile_id == profile, Connection.channel == "telegram"
                )
            )
            if not VALIDATORS["telegram"].enabled or state != ConnectionState.LIVE:
                raise RuntimeError("Подключение Telegram отключено")
            return normalize_telegram_target(decrypt_blob(vault, matches[0].value_blob))
    finally:
        await engine.dispose()


async def telegram_call(function, *args, **kwargs):
    while True:
        try:
            return await function(*args, **kwargs)
        except FloodWaitError as error:
            print(f"FloodWait: ожидание {error.seconds} с, затем повтор", flush=True)
            await asyncio.sleep(error.seconds)


def media_options(video: Path) -> dict:
    if ensure_thumbnail is None:
        return {}
    duration, width, height = ffprobe(video)
    thumb, _ = ensure_thumbnail(video, _sha256(video), duration, Path("runtime/thumbs"))
    return {
        "thumb": str(thumb),
        "attributes": [DocumentAttributeVideo(duration, width, height, supports_streaming=True)],
    }


async def upload(client, target, rows) -> None:
    for index, (number, video, caption) in enumerate(rows):
        options = await asyncio.to_thread(media_options, video)
        print(f"{number:03}: {video.name} — отправка", flush=True)
        message = await telegram_call(
            client.send_file,
            target,
            str(video),
            caption=caption,
            parse_mode=None,
            supports_streaming=True,
            **options,
        )
        print(
            f"{number:03}: OK message_id={message.id} https://t.me/systemyogi/{message.id}",
            flush=True,
        )
        if index + 1 < len(rows):
            await asyncio.sleep(15)


async def run(args, config, session_string) -> None:
    rows = await asyncio.to_thread(collect_pairs, SOURCE)
    rows = [row for row in rows if row[0] >= args.start]
    if args.limit is not None:
        rows = rows[: args.limit]
    target = await configured_target(args.profile)
    client = TelegramClient(
        StringSession(session_string),
        int(config.get("TG_API_ID") or config["TELEGRAM_API_ID"]),
        config.get("TG_API_HASH") or config["TELEGRAM_API_HASH"],
        request_retries=0,
        flood_sleep_threshold=0,
    )
    try:
        await client.connect()
        if not await telegram_call(client.is_user_authorized):
            raise RuntimeError("Сессия crosspost не авторизована")
        # ResolveUsername instead of enumerating all dialogs. Check both username and numeric ID.
        entity = await telegram_call(
            client.get_entity, USERNAME if target.lstrip("-").isdigit() else target
        )
        if (
            get_peer_id(entity) != CHAT_ID
            or (entity.username or "").casefold() != USERNAME[1:]
            or getattr(entity, "forum", False)
        ):
            raise RuntimeError("Цель не совпадает с разрешённым каналом @systemyogi")
        print(
            f"Основная → {USERNAME}, chat_id={CHAT_ID}; к отправке {len(rows)} роликов", flush=True
        )
        await upload(client, entity, rows)
    finally:
        await client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=int, default=1, help="первый номер для отправки (1–69)")
    parser.add_argument("--limit", type=int, help="число роликов; --limit 1 для пробы")
    parser.add_argument("--profile", type=int, default=1, help="ID профиля crosspost")
    args = parser.parse_args()
    if not 1 <= args.start <= 69 or args.profile < 1 or (args.limit is not None and args.limit < 1):
        parser.error("Неверный --start, --profile или --limit")
    config = load_config()
    path = Path(config.get("TG_SESSION_PATH", "runtime/sessions/telegram.session")).expanduser()
    session_string = path.read_text().strip()
    if not session_string:
        raise RuntimeError("Пустая Telegram-сессия crosspost")
    # Same session lock key as T-507. Never run two senders on one session concurrently.
    lock_dir = Path(tempfile.gettempdir()) / f"crosspost-reels-{os.getuid()}"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(("telegram-session:" + str(path.resolve())).encode()).hexdigest()
    with (lock_dir / (key + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Telegram-сессия уже используется другим процессом") from None
        asyncio.run(run(args, config, session_string))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ОСТАНОВКА: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
