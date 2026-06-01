"""
forge/db/database.py
====================
Async SQLAlchemy engine + session factory.

Default: SQLite (zero-config for local dev — no Postgres needed).
Production: set DATABASE_URL=postgresql+asyncpg://user:pass@host/db in .env

  Local dev  → sqlite+aiosqlite:///./forge.db   (auto-created on startup)
  Production → postgresql+asyncpg://...          (set in .env)
"""

import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./forge.db"
)

_is_sqlite = DATABASE_URL.startswith("sqlite")

# SQLite: no connection pool args (StaticPool / NullPool handles concurrency)
# PostgreSQL: pool_size=10, max_overflow=20 for prod throughput
if _is_sqlite:
    from sqlalchemy.pool import StaticPool
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
    )

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields a DB session and ensures it's closed."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    """Create all tables on startup (idempotent)."""
    from forge.db import models  # noqa: F401 — ensure models are registered
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
