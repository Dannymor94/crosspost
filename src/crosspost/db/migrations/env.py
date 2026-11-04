"""Alembic env.py — async SQLite через aiosqlite."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import command, context
from crosspost.db.engine import get_db_url
from crosspost.db.migration_runner import adopt_legacy
from crosspost.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return os.environ.get("DB_URL", get_db_url())


def run_migrations_offline() -> None:
    raise RuntimeError("T-201 requires an online connection to inspect the legacy schema")


def do_run_migrations(connection: Connection) -> None:
    cli_command = getattr(config.cmd_opts, "cmd", (None,))[0]
    if config.attributes.get("adopt_legacy") or cli_command is command.upgrade:
        adopt_legacy(connection, config)
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = get_url()
    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.begin() as connection:
            if connection.dialect.name == "sqlite":
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
