"""
scripts/migrate_sqlite_to_postgres.py
======================================
One-shot copy of every row from the legacy forge.db SQLite database into the
current Postgres DATABASE_URL. Idempotent on PK conflicts — re-running is safe.

Usage (run from forge-server/):
  python scripts/migrate_sqlite_to_postgres.py             # uses defaults
  python scripts/migrate_sqlite_to_postgres.py \
      --sqlite ../forge-data/forge.db                      # custom source

Expects:
  - The Postgres schema to already exist (run `alembic upgrade head` first).
  - DATABASE_URL env var pointing at the destination Postgres.

What it does:
  1. Connects to both DBs.
  2. For each table (in FK-dependency order), reads all rows from SQLite,
     casts string UUIDs → uuid.UUID, naive datetimes → UTC-aware, and inserts
     into Postgres with ON CONFLICT (id) DO NOTHING.
  3. Prints a per-table count summary.

What it does NOT do:
  - Delete the SQLite file (keep it as a safety net for a few days).
  - Run schema migrations (Alembic's job).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Allow running as `python scripts/migrate_sqlite_to_postgres.py` from forge-server/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge_server.config import get_settings  # noqa: E402


# Tables in dependency order — parents first so FKs resolve.
TABLES_IN_ORDER = [
    "users",
    "user_settings",
    "user_provider_keys",
    "projects",
    "dev_containers",
    "supabase_connections",
    "project_env_vars",
]

# Columns that hold UUIDs (must be cast str → uuid.UUID).
UUID_COLUMNS = {
    "users":                {"id"},
    "user_settings":        {"id", "user_id"},
    "user_provider_keys":   {"id", "user_id"},
    "projects":             {"id", "user_id"},
    "dev_containers":       {"id", "project_id"},
    "supabase_connections": {"id", "project_id"},
    "project_env_vars":     {"id", "project_id"},
}

# Columns that hold timestamps (str/datetime → UTC-aware datetime).
DATETIME_COLUMNS = {
    "users":                {"created_at"},
    "user_settings":        {"updated_at"},
    "user_provider_keys":   {"created_at", "updated_at"},
    "projects":             {"created_at", "updated_at", "showcased_at"},
    "dev_containers":       {"created_at", "updated_at", "started_at", "last_ping_at"},
    "supabase_connections": {"connected_at", "last_used_at"},
    "project_env_vars":     {"created_at", "updated_at"},
}

# Columns that hold booleans. SQLite stores them as int 0/1 — asyncpg is
# strict about BOOLEAN columns and refuses int values, so we coerce here.
BOOLEAN_COLUMNS = {
    "users":            {"email_verified", "onboarding_completed"},
    "project_env_vars": {"inject_runtime"},
}


def _strip_legacy_prefixes(s: str) -> str:
    """
    Legacy IDs in the old SQLite DB sometimes had human-readable prefixes
    like "dev-user-<uuid>" (see forge_server.config.dev_user_id pre-2026-05).
    Postgres UUID columns can't store those, so strip the prefix here. Any
    other prefix → leave as-is (the UUID parse below will fail loud).
    """
    if s.startswith("dev-user-"):
        return s[len("dev-user-"):]
    return s


def _coerce_uuid(v: Any) -> Any:
    if v is None or isinstance(v, uuid.UUID):
        return v
    return uuid.UUID(_strip_legacy_prefixes(str(v)))


def _coerce_datetime(v: Any) -> Any:
    """SQLite stored naive UTC datetimes (per datetime.utcnow). Attach UTC tz."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    # SQLite sometimes hands back strings — parse them.
    try:
        # Try ISO with microseconds; fall back to without.
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    except Exception:
        pass
    raise ValueError(f"Cannot parse datetime: {v!r}")


def _coerce_bool(v: Any) -> Any:
    """SQLite hands us int 0/1 for BOOLEAN columns; asyncpg wants real bool."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "yes", "y")
    return bool(v)


def _coerce_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """Cast SQLite values to Postgres-friendly Python types."""
    out = dict(row)
    for col in UUID_COLUMNS.get(table, ()):
        if col in out:
            out[col] = _coerce_uuid(out[col])
    for col in DATETIME_COLUMNS.get(table, ()):
        if col in out:
            out[col] = _coerce_datetime(out[col])
    for col in BOOLEAN_COLUMNS.get(table, ()):
        if col in out:
            out[col] = _coerce_bool(out[col])
    # Rewrite embedded path strings that contained the old "dev-user-<uuid>"
    # directory name so they line up with the renamed on-disk directory.
    if table == "projects" and "workspace_path" in out and out["workspace_path"]:
        out["workspace_path"] = out["workspace_path"].replace(
            "/users/dev-user-", "/users/"
        )
    return out


async def _copy_table(sqlite_conn: aiosqlite.Connection, pg_conn, table: str) -> int:
    """Returns count of rows actually inserted (excludes ON CONFLICT skips)."""
    sqlite_conn.row_factory = aiosqlite.Row
    cur = await sqlite_conn.execute(f"SELECT * FROM {table}")
    rows = await cur.fetchall()
    if not rows:
        return 0

    coerced = [_coerce_row(table, dict(r)) for r in rows]
    cols = list(coerced[0].keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(f'"{c}"' for c in cols)

    # ON CONFLICT DO NOTHING (no target) skips on ANY constraint violation —
    # PK *or* unique. Matters when forge-server's lifespan has already
    # auto-created the dev user with the same email/username this script is
    # about to insert.
    sql = text(
        f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
        f'ON CONFLICT DO NOTHING'
    )
    inserted = 0
    for row in coerced:
        result = await pg_conn.execute(sql, row)
        inserted += result.rowcount or 0
    return inserted


async def migrate(sqlite_path: Path, postgres_url: str) -> None:
    print(f"[migrate] SQLite source:    {sqlite_path}")
    print(f"[migrate] Postgres target:  {_redact(postgres_url)}")
    if not sqlite_path.exists():
        print(f"[migrate] ⚠ Source DB not found — nothing to migrate. Exiting clean.")
        return

    engine = create_async_engine(postgres_url, echo=False)
    sqlite_conn = await aiosqlite.connect(str(sqlite_path))

    try:
        async with engine.begin() as pg_conn:
            for table in TABLES_IN_ORDER:
                # Skip tables that don't exist in the source (e.g. fresh dev box).
                check = await sqlite_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                if not await check.fetchone():
                    print(f"[migrate] {table:<22} — not in source, skipped")
                    continue
                inserted = await _copy_table(sqlite_conn, pg_conn, table)
                print(f"[migrate] {table:<22} → {inserted} rows inserted")
        print("[migrate] ✓ Done.")
    finally:
        await sqlite_conn.close()
        await engine.dispose()


def _redact(url: str) -> str:
    """Hide the password in a URL for logging."""
    import re
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite",
        default=os.environ.get("FORGE_LEGACY_SQLITE", "../forge-data/forge.db"),
        help="Path to the legacy forge.db SQLite file",
    )
    parser.add_argument(
        "--postgres-url",
        default=None,
        help="Override DATABASE_URL for the target Postgres",
    )
    args = parser.parse_args()

    settings = get_settings()
    pg_url = args.postgres_url or settings.database_url
    if not pg_url.startswith("postgresql"):
        print(
            f"[migrate] ✗ DATABASE_URL is not Postgres: {_redact(pg_url)}\n"
            f"[migrate]    Set DATABASE_URL to a postgresql+asyncpg:// URL before running.",
            file=sys.stderr,
        )
        sys.exit(2)

    sqlite_path = Path(args.sqlite).resolve()
    asyncio.run(migrate(sqlite_path, pg_url))


if __name__ == "__main__":
    main()
