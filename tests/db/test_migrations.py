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
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "t216_operation_ledger",
        )


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


def _sqlite_schema(path):
    """Семантика всей схемы; FK читаем PRAGMA, минуя reflection inline-DDL.

    Порядок колонок после batch rebuild и технические имена autoindex/FK
    не влияют на ограничения; имена обычных индексов проверяем.
    """
    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            result = {}
            for table in inspector.get_table_names():
                if table == "alembic_version":
                    continue
                columns = []
                for _, name, kind, notnull, default, pk in conn.exec_driver_sql(
                    f'PRAGMA table_info("{table}")'
                ):
                    columns.append(
                        (name, kind, notnull, default.strip("()") if default else default, pk)
                    )
                indexes = []
                for _, name, unique, origin, partial in conn.exec_driver_sql(
                    f'PRAGMA index_list("{table}")'
                ):
                    fields = [
                        tuple(row[2:])
                        for row in conn.exec_driver_sql(f'PRAGMA index_xinfo("{name}")')
                    ]
                    indexes.append((name if origin == "c" else origin, unique, partial, fields))
                result[table] = {
                    "columns": sorted(columns),
                    "foreign_keys": sorted(
                        tuple(row[1:])
                        for row in conn.exec_driver_sql(f'PRAGMA foreign_key_list("{table}")')
                    ),
                    "indexes": sorted(indexes),
                    "unique": sorted(
                        (c["name"] or "", tuple(c["column_names"]))
                        for c in inspector.get_unique_constraints(table)
                    ),
                    "checks": inspector.get_check_constraints(table),
                }
            return result
    finally:
        engine.dispose()


def _old_database(path, variant):
    with sqlite3.connect(path) as conn:
        fixtures = Path(__file__).parent / "fixtures"
        conn.executescript((fixtures / "ede92c0.sql").read_text())
        if variant != "create_all":
            conn.executescript((fixtures / "manual_publications.sql").read_text())
        if variant == "before_alters":
            conn.execute("DROP INDEX ix_scheduled_report")
            for table, columns in {
                "connections": ["expires_at"],
                "scheduled_posts": ["published_at", "article_text", "link_phrase", "reject_reason"],
                "channel_targets": ["topic_id", "topic_title", "peer_access_hash"],
            }.items():
                for column in columns:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        conn.execute("INSERT INTO profiles(name) VALUES ('client')")
        conn.execute(
            "INSERT INTO publications(profile_id,publication_id,channel,target,status,external_id,attempt_count,created_at,updated_at) VALUES (1,'post','telegram','','done','receipt',3,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO users(login,password_hash,role,profile_id) VALUES ('client','test-hash','client',1)"
        )


@pytest.mark.parametrize("variant", ["create_all", "manual_rebuild", "before_alters"])
async def test_entire_legacy_schema_matches_fresh_models(tmp_path, variant):
    from sqlalchemy import create_engine

    old_path = tmp_path / "legacy.sqlite"
    reference_path = tmp_path / "models.sqlite"
    _old_database(old_path, variant)
    reference = create_engine(f"sqlite:///{reference_path}")
    with reference.begin() as conn:
        Base.metadata.create_all(conn)
    reference.dispose()
    expected = _sqlite_schema(reference_path)
    for _ in range(2):
        engine = await create_engine_and_tables(f"sqlite+aiosqlite:///{old_path}")
        await engine.dispose()
        assert _sqlite_schema(old_path) == expected
    with sqlite3.connect(old_path) as conn:
        assert conn.execute(
            "SELECT publication_id,external_id,attempt_count FROM publications"
        ).fetchall() == [("post", "receipt", 3)]
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM profiles WHERE id=1")
        assert conn.execute("SELECT * FROM publications").fetchall() == []
        assert conn.execute("SELECT profile_id FROM users").fetchall() == [(None,)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_failure_after_rebuild_rolls_back_and_can_retry(tmp_path):
    from sqlalchemy import Engine, event

    path = tmp_path / "rollback.sqlite"
    _old_database(path, "before_alters")
    with sqlite3.connect(path) as conn:
        before = list(conn.iterdump())
    statements = []

    def fail_after_rebuild(conn, cursor, statement, parameters, context, many):
        statements.append(statement.strip())
        if statement.startswith("UPDATE scheduled_posts SET status="):
            raise RuntimeError("injected failure after rebuild")

    event.listen(Engine, "before_cursor_execute", fail_after_rebuild)
    try:
        with pytest.raises(RuntimeError, match="injected failure after rebuild"):
            await create_engine_and_tables(f"sqlite+aiosqlite:///{path}")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_rebuild)
    assert "DROP TABLE publications" in statements
    with sqlite3.connect(path) as conn:
        assert list(conn.iterdump()) == before
    engine = await create_engine_and_tables(f"sqlite+aiosqlite:///{path}")
    await engine.dispose()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "t216_operation_ledger",
        )
        assert conn.execute("PRAGMA foreign_key_list(publications)").fetchone()[6] == "CASCADE"
