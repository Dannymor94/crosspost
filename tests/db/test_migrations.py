"""Проверки перехода со старой БД на единственную систему миграций."""

import sqlite3
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from crosspost.db.engine import create_engine_and_tables
from crosspost.db.models import Base


@pytest.mark.parametrize("legacy", [False, True])
async def test_upgrade_schema_and_restart(tmp_path, legacy):
    path = tmp_path / "db.sqlite"
    if legacy:
        with sqlite3.connect(path) as conn:
            conn.executescript((Path(__file__).parent / "fixtures/ede92c0.sql").read_text())
            conn.execute("INSERT INTO profiles(name) VALUES ('client')")
    url = f"sqlite+aiosqlite:///{path}"
    for _ in range(2):
        engine = await create_engine_and_tables(url)
        try:
            async with engine.connect() as conn:
                assert await conn.scalar(text("SELECT version_num FROM alembic_version"))
                diffs = await conn.run_sync(
                    lambda c: compare_metadata(
                        MigrationContext.configure(c, opts={"compare_server_default": True}),
                        Base.metadata,
                    )
                )
                assert diffs == []
                if legacy:
                    assert await conn.scalar(text("SELECT name FROM profiles")) == "client"
        finally:
            await engine.dispose()


@pytest.mark.parametrize("versioned", [False, True])
async def test_initial_revision_upgrade_preserves_publications(tmp_path, versioned):
    from sqlalchemy import create_engine

    from alembic import command
    from crosspost.db.migration_runner import migration_config

    path = tmp_path / "initial.sqlite"
    sync_engine = create_engine(f"sqlite:///{path}")
    with sync_engine.begin() as conn:
        config = migration_config()
        config.attributes["connection"] = conn
        command.upgrade(config, "85b6d4dfff8f")
        if not versioned:
            conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO profiles(name) VALUES ('client')"))
        conn.execute(
            text(
                "INSERT INTO publications(profile_id,publication_id,channel,status,external_id,attempt_count,updated_at) VALUES (1,'post','telegram','done','receipt',3,CURRENT_TIMESTAMP)"
            )
        )
    sync_engine.dispose()
    engine = await create_engine_and_tables(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT publication_id, target, external_id, attempt_count FROM publications"
                    )
                )
            ).one()
            assert tuple(row) == ("post", "", "receipt", 3)
            assert (
                await conn.run_sync(
                    lambda c: compare_metadata(
                        MigrationContext.configure(c, opts={"compare_server_default": True}),
                        Base.metadata,
                    )
                )
                == []
            )
    finally:
        await engine.dispose()


async def test_unknown_schema_refused_without_stamp(tmp_path):
    path = tmp_path / "unknown.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE profiles(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO profiles VALUES (1, 'keep')")
    with pytest.raises(RuntimeError, match="Unrecognized legacy database"):
        await create_engine_and_tables(f"sqlite+aiosqlite:///{path}")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM profiles").fetchall() == [(1, "keep")]
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name='alembic_version'"
        ).fetchall()


async def test_cli_upgrades_ede92c0(tmp_path, monkeypatch):
    import asyncio
    import sys

    path = tmp_path / "cli.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript((Path(__file__).parent / "fixtures/ede92c0.sql").read_text())
    monkeypatch.setenv("DB_URL", f"sqlite+aiosqlite:///{path}")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, (stdout, stderr)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("t201_unify",)


async def test_manual_migration_columns_and_nullable_legacy_rows(tmp_path):
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    path = tmp_path / "manual.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript((Path(__file__).parent / "fixtures/ede92c0.sql").read_text())
        conn.execute("DROP INDEX ix_scheduled_report")
        for table, columns in {
            "connections": ["expires_at"],
            "scheduled_posts": ["published_at", "article_text", "link_phrase", "reject_reason"],
            "channel_targets": ["topic_id", "topic_title", "peer_access_hash"],
        }.items():
            for column in columns:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    sync_engine = create_engine(f"sqlite:///{path}")
    with sync_engine.begin() as conn:
        ops = Operations(MigrationContext.configure(conn))
        with ops.batch_alter_table("publications") as batch:
            batch.alter_column("created_at", nullable=True, server_default=None)
            batch.alter_column("updated_at", nullable=True)
            batch.alter_column("target", server_default="")
            batch.alter_column("attempt_count", server_default="0")
        conn.execute(text("INSERT INTO profiles(name) VALUES ('client')"))
        conn.execute(
            text(
                "INSERT INTO publications(profile_id,publication_id,channel,status,external_id) VALUES (1,'schedpost-1','telegram','failed','receipt')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO scheduled_posts(id,profile_id,content_type,text,media_paths,channels,scheduled_at,status) VALUES (1,1,'post','keep','[]','[]',CURRENT_TIMESTAMP,'published')"
            )
        )
        conn.execute(text("INSERT INTO checkpoints VALUES (1,'post','telegram','','checkpoint')"))
        conn.execute(
            text(
                "INSERT INTO credentials(profile_id,channel,kind,blob,updated_at) VALUES (1,'telegram','api_token','opaque-fixture',CURRENT_TIMESTAMP)"
            )
        )
    sync_engine.dispose()
    engine = await create_engine_and_tables(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT status FROM scheduled_posts")) == "failed"
            assert await conn.scalar(text("SELECT external_id FROM publications")) == "receipt"
            assert await conn.scalar(text("SELECT external_id FROM checkpoints")) == "checkpoint"
            assert await conn.scalar(text("SELECT blob FROM credentials")) == "opaque-fixture"
            assert (
                await conn.run_sync(
                    lambda c: compare_metadata(
                        MigrationContext.configure(c, opts={"compare_server_default": True}),
                        Base.metadata,
                    )
                )
                == []
            )
    finally:
        await engine.dispose()
