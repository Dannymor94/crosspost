"""Общий запуск Alembic для приложения и CLI."""

from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from alembic import command

INITIAL_REVISION = "85b6d4dfff8f"


def migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes["adopt_legacy"] = True
    return config


def adopt_legacy(connection: Connection, config: Config) -> None:
    """Штампуем только распознанную базовую схему, никогда сразу head."""
    ctx = MigrationContext.configure(connection)
    if ctx.get_current_heads():
        return
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names()) - {"alembic_version"}
    if not tables:
        return
    required = {
        "profiles": {"id", "name", "created_at"},
        "connections": {"id", "profile_id", "channel", "state", "updated_at"},
        "credentials": {"id", "profile_id", "channel", "kind", "blob", "updated_at"},
        "logs": {
            "id",
            "profile_id",
            "channel",
            "publication_id",
            "level",
            "message",
            "screenshot_path",
            "created_at",
        },
        "publications": {
            "id",
            "profile_id",
            "publication_id",
            "channel",
            "status",
            "external_id",
            "error",
            "attempt_count",
            "created_at",
            "updated_at",
        },
    }
    for table, columns in required.items():
        if table not in tables or not columns <= {c["name"] for c in inspector.get_columns(table)}:
            raise RuntimeError(f"Unrecognized legacy database schema: {table}; migration refused")
    unique_keys = {
        "profiles": ["name"],
        "connections": ["profile_id", "channel"],
        "credentials": ["profile_id", "channel", "kind"],
        "publications": ["profile_id", "publication_id", "channel"],
    }
    if "target" in {c["name"] for c in inspector.get_columns("publications")}:
        unique_keys["publications"].append("target")
    for table, key in unique_keys.items():
        constraints = inspector.get_unique_constraints(table)
        if not any(c["column_names"] == key for c in constraints):
            raise RuntimeError(
                f"Unrecognized legacy database uniqueness: {table}; migration refused"
            )
    ctx.stamp(ScriptDirectory.from_config(config), INITIAL_REVISION)


def upgrade_database(connection: Connection) -> None:
    config = migration_config()
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
