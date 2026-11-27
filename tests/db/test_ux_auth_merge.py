"""Rehearsal of the auth/UX migration graph on disposable SQLite only."""

import sqlite3

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from alembic import command
from crosspost.db.engine import create_engine_and_tables
from crosspost.db.migration_runner import migration_config
from crosspost.db.models import Base


def test_rehearsal_has_single_head_and_preserves_original_parents():
    script = ScriptDirectory.from_config(migration_config())
    assert script.get_heads() == ["t406_ux_merge"]
    assert script.get_revision("t406_ux_merge").down_revision == (
        "t406_session_generation",
        "t218_editor_draft",
    )
    assert script.get_revision("t406_session_generation").down_revision == "t203_fencing"
    assert script.get_revision("t218_editor_draft").down_revision == "t217_schedule_utc"


@pytest.mark.parametrize(
    "start",
    ["t203_fencing", "t406_session_generation", "t218_editor_draft"],
)
async def test_auth_ux_upgrade_preserves_existing_state(tmp_path, start):
    path = tmp_path / "existing.sqlite"
    sync_engine = create_engine(f"sqlite:///{path}")
    with sync_engine.begin() as conn:
        config = migration_config()
        config.attributes["connection"] = conn
        command.upgrade(config, start)
        conn.execute(text("INSERT INTO profiles(id, name) VALUES (1, 'existing')"))
        conn.execute(
            text(
                "INSERT INTO users(id, login, password_hash, role, profile_id) "
                "VALUES (1, 'existing', 'fixture-hash', 'client', 1)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO publications(profile_id, publication_id, channel, target, "
                "status, external_id, attempt_count, created_at, updated_at) "
                "VALUES (1, 'prior', 'telegram', '', 'done', 'receipt', 2, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        if start == "t406_session_generation":
            conn.execute(text("UPDATE users SET session_generation=7 WHERE id=1"))
        if start == "t218_editor_draft":
            conn.execute(
                text(
                    "INSERT INTO editor_drafts(scope, user_id, profile_id, revision, data) "
                    "VALUES ('fixture', 1, 1, 'rev', '{}')"
                )
            )
    sync_engine.dispose()

    for _ in range(2):
        engine = await create_engine_and_tables(f"sqlite+aiosqlite:///{path}")
        try:
            async with engine.connect() as conn:
                assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == (
                    "t406_ux_merge"
                )
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

    with sqlite3.connect(path) as conn:
        generation = 7 if start == "t406_session_generation" else 0
        assert conn.execute(
            "SELECT login, password_hash, profile_id, session_generation FROM users"
        ).fetchall() == [("existing", "fixture-hash", 1, generation)]
        assert conn.execute(
            "SELECT publication_id, external_id, attempt_count FROM publications"
        ).fetchall() == [("prior", "receipt", 2)]
        expected_drafts = [("fixture", "rev", "{}")] if start == "t218_editor_draft" else []
        assert conn.execute(
            "SELECT scope, revision, data FROM editor_drafts"
        ).fetchall() == expected_drafts
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
