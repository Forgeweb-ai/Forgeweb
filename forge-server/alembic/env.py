"""
alembic/env.py — async-aware migration runner.

Loads DATABASE_URL from forge_server.config (which reads from .env / env vars).
Falls back to synchronous mode for offline migration generation.

Run migrations:
  cd forge-server && alembic upgrade head
Generate a new migration from model changes:
  cd forge-server && alembic revision --autogenerate -m "describe change"
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import all models so Base.metadata is fully populated for autogenerate.
from forge_server.config import get_settings
from forge_server.db.database import Base
from forge_server.db import models  # noqa: F401  (registers models on Base)

config = context.config

# Inject the runtime DATABASE_URL into Alembic's config so it doesn't have to
# live in alembic.ini (where it'd be checked into git and diverge from .env).
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL without a live connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against a live async connection."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
