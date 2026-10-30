"""Throwaway diagnostic: проверяет отправку сообщений/файлов в топик форума Telegram.

Usage:
    uv run python scripts/tg_topic_probe.py \
        --profile-id 1 --chat -1001234567890 --topic 123 --video /path/to/test.mp4
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Add src/ to path so we can import crosspost without installing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-id", type=int, required=True)
    parser.add_argument("--chat", required=True, help="Numeric chat id, e.g. -1001234567890")
    parser.add_argument("--topic", type=int, required=True, help="Forum topic (message thread) id")
    parser.add_argument("--video", required=True, help="Path to a small .mp4 file")
    args = parser.parse_args()

    chat_id = int(args.chat)

    # ── Load credentials from DB ───────────────────────────────────────────────
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from crosspost.channels.telegram_login import parse_credential_blob
    from crosspost.db.engine import get_db_url
    from crosspost.db.models import CredentialKind
    from crosspost.db.vault import get_vault

    vault = get_vault()
    engine = create_async_engine(get_db_url(), echo=False)

    import sqlalchemy as sa

    from crosspost.db.models import Credential

    async with AsyncSession(engine) as session:
        row = await session.scalar(
            sa.select(Credential.blob).where(
                Credential.profile_id == args.profile_id,
                Credential.channel == "telegram",
                Credential.kind == CredentialKind.API_TOKEN,
            )
        )

    if row is None:
        print(f"FAIL  credentials: no telegram API_TOKEN for profile_id={args.profile_id}")
        return

    from crosspost.db.vault import decrypt_blob

    blob = decrypt_blob(vault, row)
    creds = parse_credential_blob(blob)
    if not creds.get("session"):
        print("FAIL  credentials: blob parsed but 'session' key missing")
        return

    print("PASS  credentials loaded (not printed)")

    # ── Build client ───────────────────────────────────────────────────────────
    from telethon import TelegramClient, types
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(creds["session"]),
        int(creds["api_id"]),
        creds["api_hash"],
    )
    await client.connect()
    if not await client.is_user_authorized():
        print("FAIL  client: session expired / not authorised")
        await client.disconnect()
        return
    print("PASS  client connected and authorised")

    # ── A: get_entity without get_dialogs ─────────────────────────────────────
    entity = None
    try:
        entity = await client.get_entity(chat_id)
        print(
            f"PASS  A: get_entity → {type(entity).__name__}  id={entity.id}  access_hash={entity.access_hash}"
        )
    except Exception as exc:
        print(f"FAIL  A: get_entity → {exc}")

    # ── B: retry after get_dialogs ────────────────────────────────────────────
    if entity is None:
        try:
            await client.get_dialogs()
            entity = await client.get_entity(chat_id)
            print(
                f"PASS  B: get_entity after get_dialogs → id={entity.id}  access_hash={entity.access_hash}"
            )
        except Exception as exc:
            print(f"FAIL  B: get_entity after get_dialogs → {exc}")

    if entity is None:
        print("SKIP  C/D/E: no entity resolved")
        await client.disconnect()
        return

    # ── C: send_message with reply_to=int ────────────────────────────────────
    try:
        msg = await client.send_message(entity, "probe: text → topic", reply_to=args.topic)
        print(f"PASS  C: send_message reply_to=int → message_id={msg.id}")
    except Exception as exc:
        print(f"FAIL  C: send_message reply_to=int → {exc}")

    # ── D: send_file with reply_to=int ────────────────────────────────────────
    try:
        msg = await client.send_file(
            entity,
            args.video,
            caption="probe: video → topic",
            reply_to=args.topic,
            supports_streaming=True,
        )
        mid = msg[0].id if isinstance(msg, list) else msg.id
        print(f"PASS  D: send_file reply_to=int → message_id={mid}")
    except Exception as exc:
        print(f"FAIL  D: send_file reply_to=int → {exc}")

    # ── E: send_message with InputReplyToMessage (top_msg_id) ─────────────────
    try:
        reply_to_obj = types.InputReplyToMessage(
            reply_to_msg_id=args.topic,
            top_msg_id=args.topic,
        )
        msg = await client.send_message(entity, "probe: InputReplyToMessage", reply_to=reply_to_obj)
        print(f"PASS  E: InputReplyToMessage → message_id={msg.id}  (unexpectedly succeeded)")
    except Exception as exc:
        print(f"FAIL  E: InputReplyToMessage → {exc}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
