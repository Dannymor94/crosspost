"""One-off: set topic_title for channel_targets rows where it's missing.

Usage:
    uv run python scripts/backfill_topic_title.py --id 4 --title "Книга Ты не поломан"
    uv run python scripts/backfill_topic_title.py --id 4 --title "Книга Ты не поломан" --db runtime/db.sqlite3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", type=int, required=True, help="channel_targets.id")
    parser.add_argument("--title", required=True, help="topic_title to set")
    parser.add_argument("--db", default="runtime/db.sqlite3", help="SQLite DB path")
    args = parser.parse_args()

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    url = f"sqlite+aiosqlite:///{args.db}"
    engine = create_async_engine(url, echo=False)

    async with engine.begin() as conn:
        result = await conn.execute(
            sa.text("SELECT id, name, topic_id, topic_title FROM channel_targets WHERE id = :id"),
            {"id": args.id},
        )
        row = result.fetchone()
        if row is None:
            print(f"ERROR: target id={args.id} not found")
            return
        print(
            f"Before: id={row.id} name={row.name!r} topic_id={row.topic_id} topic_title={row.topic_title!r}"
        )

        await conn.execute(
            sa.text("UPDATE channel_targets SET topic_title = :title WHERE id = :id"),
            {"title": args.title, "id": args.id},
        )
        print(f"Updated topic_title → {args.title!r}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
