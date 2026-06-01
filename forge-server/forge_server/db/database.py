"""
forge_server/db/database.py
============================
Async SQLAlchemy engine + session factory.

Schema is owned by Alembic — see forge-server/alembic/. Apply with:
  alembic upgrade head
(dev.sh runs this automatically before starting forge-server.)

DATABASE_URL must be a Postgres URL — Postgres is the only supported backend
since the SQLite → Supabase Postgres migration. See [[forge_storage_architecture]].
  Local dev:  postgresql+asyncpg://postgres:postgres@127.0.0.1:54322/postgres
  Production: postgresql+asyncpg://...pooler.supabase.com:6543/postgres
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from forge_server.config import get_settings

settings = get_settings()


def _make_engine():
    url = settings.database_url
    if not url.startswith(("postgresql", "postgres")):
        raise RuntimeError(
            f"Unsupported DATABASE_URL: {url!r}. "
            "forge-server now requires Postgres. Run dev.sh which boots local Supabase, "
            "or set DATABASE_URL to a postgresql+asyncpg:// URL."
        )
    return create_async_engine(
        url,
        pool_size=20,
        max_overflow=40,
        pool_pre_ping=True,
        echo=False,
    )


engine = _make_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:  # type: ignore[misc]
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """
    No-op kept for API compatibility with code that calls it on startup.

    Schema lifecycle now lives in Alembic — dev.sh runs `alembic upgrade head`
    before starting forge-server, and production deploys run the same.

    The previous version of this function called Base.metadata.create_all
    plus a list of try/except ALTER TABLE statements. Both are gone: Alembic
    owns the schema, and forge-server should never silently mutate it on boot.
    """
    return None
