"""
forge_server/api/db_routes.py
==============================
Data tab API — introspect and edit the project's database.

Endpoints:
  GET    /api/projects/{id}/db/info               — driver + status
  GET    /api/projects/{id}/db/tables             — list tables + schemas + row counts
  GET    /api/projects/{id}/db/tables/{name}      — paginated rows
  POST   /api/projects/{id}/db/tables/{name}/rows — insert a row
  PATCH  /api/projects/{id}/db/tables/{name}/rows/{pk}
  DELETE /api/projects/{id}/db/tables/{name}/rows/{pk}
  POST   /api/projects/{id}/db/sql                — execute raw SQL (read by default; ?write=1 to allow writes)
  POST   /api/projects/{id}/db/migrate-to-supabase — kick off SQLite → Postgres migration job
  GET    /api/projects/{id}/db/migrate-to-supabase/status — poll job status

Design:
  - We open the SQLite file READ-ONLY by default so the dev container can
    keep its write lock. Mutating endpoints use a short-lived write
    connection with WAL + immediate-mode.
  - Only the project owner can hit these endpoints (current_user check).
  - Migrate job is launched as a BackgroundTask; status persists on the
    DevContainer row (or a dedicated migration_jobs table — we use the
    existing project row's `last_migration` JSON for v1).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import Project, SupabaseConnection, User

log      = logging.getLogger("forge.db")
settings = get_settings()
router   = APIRouter(prefix="/api/projects", tags=["db"])

# In-memory migration job tracker.  v1 — replace with a table if we need
# durability across restarts.
_MIGRATION_JOBS: dict[str, dict[str, Any]] = {}


# ── Schemas ───────────────────────────────────────────────────────────────────

class ColumnInfo(BaseModel):
    name:        str
    type:        str
    nullable:    bool
    primary_key: bool
    default:     str | None = None


class TableInfo(BaseModel):
    name:      str
    columns:   list[ColumnInfo]
    row_count: int


class TablesResponse(BaseModel):
    driver: str            # "sqlite" or "postgres"
    tables: list[TableInfo]


class RowsResponse(BaseModel):
    columns:   list[str]
    rows:      list[dict[str, Any]]
    total:     int
    limit:     int
    offset:    int


class SqlRequest(BaseModel):
    sql: str


class SqlResponse(BaseModel):
    columns:        list[str]
    rows:           list[list[Any]]
    rows_affected:  int


class MigrateJob(BaseModel):
    job_id: str
    status: str       # queued | running | succeeded | failed
    progress: int     # 0-100
    message: str
    started_at: float | None = None
    finished_at: float | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_owned_project(project_id: str, user: User, db: AsyncSession) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    p = result.scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Project not found")
    return p


def _db_path(project: Project) -> Path:
    return Path(project.workspace_path) / "data.db"


def _open_ro(project: Project) -> sqlite3.Connection:
    p = _db_path(project)
    if not p.exists():
        raise HTTPException(404, "data.db not found — has the dev container been started yet?")
    # uri=True lets us pass mode=ro
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _open_rw(project: Project) -> sqlite3.Connection:
    p = _db_path(project)
    if not p.exists():
        raise HTTPException(404, "data.db not found")
    conn = sqlite3.connect(str(p), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _is_internal_table(name: str) -> bool:
    """sqlite_*, drizzle's internal __drizzle_migrations, etc."""
    if name.startswith("sqlite_"):
        return True
    if name.startswith("__drizzle"):
        return True
    return False


# ── Postgres-per-schema helpers (Phase B / D9) ────────────────────────────────
# Every query endpoint below routes to these when the project has a
# provisioned-local SupabaseConnection row. BYO Supabase rows are NOT handled
# here — those apps talk to Supabase directly via the user's anon key, and
# the DataPanel deep-links into Supabase Studio for management.
#
# One asyncpg connection per request (no pool). At local-self-host scale
# (single user, dozens of projects) the connect cost is negligible vs. the
# complexity of managing per-project pools in forge-server. Phase B+ can add
# pooling if profiling shows it matters.

from contextlib import asynccontextmanager


async def _get_provisioned_pg(project_id: str, db: AsyncSession) -> SupabaseConnection | None:
    """
    Return the SupabaseConnection row for this project IFF it's a Forge-
    provisioned-local schema. None for BYO Supabase or unconnected projects.
    Indexed lookup on (project_id, provisioned_locally) — cheap to call on
    every request.
    """
    sc = (await db.execute(
        select(SupabaseConnection).where(
            SupabaseConnection.project_id == project_id,
            SupabaseConnection.provisioned_locally.is_(True),
        )
    )).scalar_one_or_none()
    return sc


def _pg_connstr(sc: SupabaseConnection) -> str:
    """
    Build an asyncpg connection string from a provisioned row.
    The role's password is decrypted on demand — never cached in memory.
    """
    from forge_server.api.supabase_routes import _fernet
    pw = _fernet().decrypt((sc.role_password_enc or "").encode()).decode()
    # Strip SQLAlchemy "+asyncpg" suffix; carry only host:port/db forward.
    base = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    host_and_db = base.split("://", 1)[1].split("@", 1)[-1]
    return f"postgresql://{sc.role_name}:{pw}@{host_and_db}"


@asynccontextmanager
async def _pg_conn(sc: SupabaseConnection):
    """
    Async context manager yielding an asyncpg connection with search_path
    set to the project's schema. Every unqualified table reference inside
    the `async with` block resolves to the right schema with no prefix.

    We SET search_path via a separate execute rather than baking it into the
    URL's `options=` because asyncpg's URL parser is finicky about it.
    """
    import asyncpg
    conn = await asyncpg.connect(_pg_connstr(sc))
    try:
        # Quote the schema identifier so an odd name (shouldn't happen, but
        # belt-and-braces) can't break out.
        await conn.execute(f'SET search_path TO "{sc.schema_name}"')
        yield conn
    finally:
        await conn.close()


def _pg_table_exists_query() -> str:
    """Reused across endpoints — table existence check scoped to schema."""
    return "SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname=$1 AND tablename=$2)"


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/{project_id}/db/info")
async def db_info(
    project_id: str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Reports the project's current DB state. Single call, one row lookup —
    cheap enough to hit on every Data tab mount.

    Response always includes the local SQLite snapshot (the prototype DB
    keeps existing even after Supabase connection — see D4 in LAUNCH_PLAN:
    SQLite is prototype-only, migration moves the data but the file may
    still be around). When a SupabaseConnection row exists for the project,
    we add `supabase` block so the FE can swap header + CTAs.
    """
    project = await _get_owned_project(project_id, user, db)
    p = _db_path(project)

    # One indexed lookup on supabase_connections.project_id (unique index).
    # No JOIN, no N+1 — same cost as the old hardcoded response.
    sc_row = await db.execute(
        select(SupabaseConnection).where(SupabaseConnection.project_id == project_id)
    )
    sc = sc_row.scalar_one_or_none()

    # Driver derivation — three states the FE needs to distinguish:
    #   - "postgres-local" : Forge provisioned a schema for the project
    #                        (local-self-host mode default once Phase B lands)
    #   - "supabase"       : user connected an external Supabase project (BYO)
    #   - "sqlite"         : legacy / Phase A holdover. New projects in Phase
    #                        B will skip this state entirely.
    if sc is None:
        driver = "sqlite"
    elif sc.provisioned_locally:
        driver = "postgres-local"
    else:
        driver = "supabase"

    return {
        "driver":     driver,
        # forge_mode lets the FE branch the "Connect Database" UX:
        # local-self-host → one-click provision; hosted → BYO Supabase OAuth.
        "forge_mode": settings.forge_mode,
        "path":       "data.db",
        "exists":     p.exists(),
        "size_bytes": p.stat().st_size if p.exists() else 0,
        # Always present so the FE can show the prototype DB alongside any
        # connected production DB. Null fields when not connected.
        "supabase":   {
            "connected":    True,
            "url":          sc.supabase_url,
            "connected_at": sc.connected_at.isoformat() if sc.connected_at else None,
            # Provisioned-local exposes the schema name so the FE can show
            # "postgres · app_xxxxxxxx" instead of the URL (which contains a
            # role password in the connstr form — never surface in chat).
            "provisioned_locally": sc.provisioned_locally,
            "schema_name":         sc.schema_name,
        } if sc is not None else {
            "connected": False, "url": None, "connected_at": None,
            "provisioned_locally": False, "schema_name": None,
        },
    }


@router.get("/{project_id}/db/tables", response_model=TablesResponse)
async def list_tables(
    project_id: str,
    include_internal: bool = Query(False),
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(project_id, user, db)

    # Phase B (D9): if the project has a provisioned-local Postgres schema,
    # list tables from there. The SQLite fallback below stays for legacy
    # projects only — new projects skip SQLite entirely.
    sc = await _get_provisioned_pg(project_id, db)
    if sc is not None:
        async with _pg_conn(sc) as pg:
            rows = await pg.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname=$1 ORDER BY tablename",
                sc.schema_name,
            )
            tables: list[TableInfo] = []
            for r in rows:
                name = r["tablename"]
                if not include_internal and _is_internal_table(name):
                    continue
                # Columns + PK detection via information_schema. One query
                # per table — N+1 in the table count, but tables-per-project
                # is small (<50 typical) and DataPanel mounts are infrequent.
                cols_raw = await pg.fetch(
                    """
                    SELECT c.column_name, c.data_type, c.is_nullable, c.column_default,
                           COALESCE((
                             SELECT TRUE FROM information_schema.key_column_usage k
                             JOIN information_schema.table_constraints t
                               ON t.constraint_name = k.constraint_name
                              AND t.table_schema    = k.table_schema
                             WHERE t.constraint_type = 'PRIMARY KEY'
                               AND k.table_schema    = c.table_schema
                               AND k.table_name      = c.table_name
                               AND k.column_name     = c.column_name
                           ), FALSE) AS is_pk
                    FROM information_schema.columns c
                    WHERE c.table_schema = $1 AND c.table_name = $2
                    ORDER BY c.ordinal_position
                    """,
                    sc.schema_name, name,
                )
                cols = [ColumnInfo(
                    name        = c["column_name"],
                    type        = c["data_type"],
                    nullable    = c["is_nullable"] == "YES",
                    primary_key = bool(c["is_pk"]),
                    default     = c["column_default"],
                ) for c in cols_raw]
                count = await pg.fetchval(f'SELECT COUNT(*) FROM "{name}"')
                tables.append(TableInfo(name=name, columns=cols, row_count=int(count or 0)))
            return TablesResponse(driver="postgres-local", tables=tables)

    # Legacy SQLite path. "No data.db yet" is the default first-run state
    # for an old SQLite project; return an empty list instead of 404 so the
    # DataPanel shows its friendly empty state rather than a red banner.
    if not _db_path(project).exists():
        return TablesResponse(driver="sqlite", tables=[])
    conn = _open_ro(project)
    try:
        names = [
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        if not include_internal:
            names = [n for n in names if not _is_internal_table(n)]

        tables: list[TableInfo] = []
        for n in names:
            cols_raw = conn.execute(f'PRAGMA table_info("{n}")').fetchall()
            cols = [
                ColumnInfo(
                    name        = c["name"],
                    type        = c["type"] or "",
                    nullable    = not c["notnull"],
                    primary_key = bool(c["pk"]),
                    default     = c["dflt_value"],
                )
                for c in cols_raw
            ]
            count_row = conn.execute(f'SELECT COUNT(*) AS c FROM "{n}"').fetchone()
            tables.append(TableInfo(name=n, columns=cols, row_count=count_row["c"]))
        return TablesResponse(driver="sqlite", tables=tables)
    finally:
        conn.close()


@router.get("/{project_id}/db/tables/{table}", response_model=RowsResponse)
async def get_rows(
    project_id: str,
    table:      str,
    limit:      int = Query(50, ge=1, le=500),
    offset:     int = Query(0, ge=0),
    order_by:   str | None = None,
    order_dir:  str = Query("asc", pattern="^(asc|desc)$"),
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(project_id, user, db)

    sc = await _get_provisioned_pg(project_id, db)
    if sc is not None:
        async with _pg_conn(sc) as pg:
            exists = await pg.fetchval(_pg_table_exists_query(), sc.schema_name, table)
            if not exists:
                raise HTTPException(404, "Table not found")
            col_rows = await pg.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=$1 AND table_name=$2 ORDER BY ordinal_position",
                sc.schema_name, table,
            )
            cols = [r["column_name"] for r in col_rows]
            if order_by and order_by not in cols:
                raise HTTPException(400, "Invalid order_by column")
            order_sql = f' ORDER BY "{order_by}" {order_dir.upper()}' if order_by else ""
            rows_raw = await pg.fetch(
                f'SELECT * FROM "{table}"{order_sql} LIMIT $1 OFFSET $2',
                limit, offset,
            )
            total = await pg.fetchval(f'SELECT COUNT(*) FROM "{table}"')
            return RowsResponse(
                columns = cols,
                rows    = [dict(r) for r in rows_raw],
                total   = int(total or 0),
                limit   = limit,
                offset  = offset,
            )

    # Legacy SQLite path
    conn = _open_ro(project)
    try:
        # validate table + column names against sqlite_master to avoid SQL injection
        valid_tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if table not in valid_tables:
            raise HTTPException(404, "Table not found")

        cols = [c["name"] for c in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        if order_by and order_by not in cols:
            raise HTTPException(400, "Invalid order_by column")

        order = f'ORDER BY "{order_by}" {order_dir.upper()}' if order_by else ""
        rows_raw = conn.execute(
            f'SELECT * FROM "{table}" {order} LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
        total = conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"]
        return RowsResponse(
            columns = cols,
            rows    = [dict(r) for r in rows_raw],
            total   = total,
            limit   = limit,
            offset  = offset,
        )
    finally:
        conn.close()


class RowMutation(BaseModel):
    values: dict[str, Any]


@router.post("/{project_id}/db/tables/{table}/rows")
async def insert_row(
    project_id: str,
    table:      str,
    body:       RowMutation,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(project_id, user, db)

    sc = await _get_provisioned_pg(project_id, db)
    if sc is not None:
        async with _pg_conn(sc) as pg:
            col_rows = await pg.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=$1 AND table_name=$2",
                sc.schema_name, table,
            )
            valid_cols = {r["column_name"] for r in col_rows}
            if not valid_cols:
                raise HTTPException(404, "Table not found")
            clean = {k: v for k, v in body.values.items() if k in valid_cols}
            if not clean:
                raise HTTPException(400, "No valid columns supplied")
            col_list     = ", ".join(f'"{c}"' for c in clean)
            placeholders = ", ".join(f"${i+1}" for i in range(len(clean)))
            try:
                row = await pg.fetchrow(
                    f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) RETURNING *',
                    *clean.values(),
                )
            except Exception as e:
                raise HTTPException(400, str(e))
            # Echo the inserted row's PK if it has one named "id" — matches
            # the existing FE contract. For tables without `id`, returns None
            # and the FE handles gracefully.
            d = dict(row) if row else {}
            return {"inserted_id": d.get("id")}

    # Legacy SQLite path
    conn = _open_rw(project)
    try:
        cols = [c["name"] for c in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        if not cols:
            raise HTTPException(404, "Table not found")
        clean = {k: v for k, v in body.values.items() if k in cols}
        if not clean:
            raise HTTPException(400, "No valid columns supplied")
        placeholders = ", ".join("?" for _ in clean)
        col_list = ", ".join(f'"{c}"' for c in clean)
        cur = conn.execute(
            f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
            tuple(clean.values()),
        )
        return {"inserted_id": cur.lastrowid}
    except sqlite3.Error as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.patch("/{project_id}/db/tables/{table}/rows/{pk}")
async def update_row(
    project_id: str,
    table:      str,
    pk:         str,
    body:       RowMutation,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(project_id, user, db)

    sc = await _get_provisioned_pg(project_id, db)
    if sc is not None:
        async with _pg_conn(sc) as pg:
            # Find PK column + all columns in one trip via key_column_usage.
            meta = await pg.fetch(
                """
                SELECT c.column_name,
                       COALESCE((
                         SELECT TRUE FROM information_schema.key_column_usage k
                         JOIN information_schema.table_constraints t
                           ON t.constraint_name = k.constraint_name
                         WHERE t.constraint_type='PRIMARY KEY'
                           AND k.table_schema=c.table_schema
                           AND k.table_name=c.table_name
                           AND k.column_name=c.column_name
                       ), FALSE) AS is_pk
                FROM information_schema.columns c
                WHERE c.table_schema=$1 AND c.table_name=$2
                """,
                sc.schema_name, table,
            )
            if not meta:
                raise HTTPException(404, "Table not found")
            pk_col   = next((r["column_name"] for r in meta if r["is_pk"]), None)
            all_cols = {r["column_name"] for r in meta}
            if pk_col is None:
                raise HTTPException(400, "Table has no primary key — cannot update by pk")
            clean = {k: v for k, v in body.values.items() if k in all_cols and k != pk_col}
            if not clean:
                raise HTTPException(400, "No valid columns supplied")
            set_clause = ", ".join(f'"{k}" = ${i+1}' for i, k in enumerate(clean))
            pk_idx     = len(clean) + 1
            try:
                result = await pg.execute(
                    f'UPDATE "{table}" SET {set_clause} WHERE "{pk_col}" = ${pk_idx}',
                    *clean.values(), pk,
                )
            except Exception as e:
                raise HTTPException(400, str(e))
            # asyncpg.execute returns "UPDATE N"
            try:
                affected = int(result.split()[-1])
            except (ValueError, IndexError):
                affected = 0
            return {"rows_affected": affected}

    # Legacy SQLite path
    conn = _open_rw(project)
    try:
        col_info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not col_info:
            raise HTTPException(404, "Table not found")
        pk_col = next((c["name"] for c in col_info if c["pk"]), "rowid")
        cols = {c["name"] for c in col_info}
        clean = {k: v for k, v in body.values.items() if k in cols}
        if not clean:
            raise HTTPException(400, "No valid columns supplied")
        set_clause = ", ".join(f'"{k}" = ?' for k in clean)
        cur = conn.execute(
            f'UPDATE "{table}" SET {set_clause} WHERE "{pk_col}" = ?',
            tuple(clean.values()) + (pk,),
        )
        return {"rows_affected": cur.rowcount}
    except sqlite3.Error as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/{project_id}/db/tables/{table}/rows/{pk}")
async def delete_row(
    project_id: str,
    table:      str,
    pk:         str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(project_id, user, db)

    sc = await _get_provisioned_pg(project_id, db)
    if sc is not None:
        async with _pg_conn(sc) as pg:
            pk_col = await pg.fetchval(
                """
                SELECT k.column_name FROM information_schema.key_column_usage k
                JOIN information_schema.table_constraints t
                  ON t.constraint_name = k.constraint_name
                WHERE t.constraint_type='PRIMARY KEY'
                  AND k.table_schema=$1 AND k.table_name=$2
                LIMIT 1
                """,
                sc.schema_name, table,
            )
            if pk_col is None:
                raise HTTPException(404, "Table not found or has no primary key")
            try:
                result = await pg.execute(
                    f'DELETE FROM "{table}" WHERE "{pk_col}" = $1', pk,
                )
            except Exception as e:
                raise HTTPException(400, str(e))
            try:
                affected = int(result.split()[-1])
            except (ValueError, IndexError):
                affected = 0
            return {"rows_affected": affected}

    # Legacy SQLite path
    conn = _open_rw(project)
    try:
        col_info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not col_info:
            raise HTTPException(404, "Table not found")
        pk_col = next((c["name"] for c in col_info if c["pk"]), "rowid")
        cur = conn.execute(f'DELETE FROM "{table}" WHERE "{pk_col}" = ?', (pk,))
        return {"rows_affected": cur.rowcount}
    except sqlite3.Error as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/{project_id}/db/sql", response_model=SqlResponse)
async def run_sql(
    project_id: str,
    body:       SqlRequest,
    write:      bool = Query(False, description="Allow write statements"),
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(project_id, user, db)
    sql = body.sql.strip()
    if not sql:
        raise HTTPException(400, "Empty SQL")

    # crude write detection — good enough for v1
    first_word = sql.split(None, 1)[0].upper()
    is_write = first_word in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "TRUNCATE"}
    if is_write and not write:
        raise HTTPException(403, "Write statements require ?write=1")

    sc = await _get_provisioned_pg(project_id, db)
    if sc is not None:
        async with _pg_conn(sc) as pg:
            try:
                if is_write:
                    # execute returns "TAG N" e.g. "UPDATE 3", "INSERT 0 5".
                    result = await pg.execute(sql)
                    try:
                        affected = int(result.split()[-1])
                    except (ValueError, IndexError):
                        affected = 0
                    return SqlResponse(columns=[], rows=[], rows_affected=affected)
                rows_raw = await pg.fetch(sql)
                if not rows_raw:
                    return SqlResponse(columns=[], rows=[], rows_affected=0)
                cols = list(rows_raw[0].keys())
                rows = [[r[c] for c in cols] for r in rows_raw]
                return SqlResponse(columns=cols, rows=rows, rows_affected=len(rows))
            except Exception as e:
                raise HTTPException(400, str(e))

    # Legacy SQLite path
    conn = _open_rw(project) if is_write else _open_ro(project)
    try:
        cur = conn.execute(sql)
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
            return SqlResponse(columns=cols, rows=rows, rows_affected=len(rows))
        return SqlResponse(columns=[], rows=[], rows_affected=cur.rowcount)
    except sqlite3.Error as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


# ── Migration to Supabase ─────────────────────────────────────────────────────

# SQLite → Postgres type translation. Covers the dialect-agnostic Drizzle
# builders the agent should be using (the db skill enforces this).
_SQLITE_TO_PG = {
    "integer":   "integer",
    "int":       "integer",
    "bigint":    "bigint",
    "real":      "double precision",
    "numeric":   "numeric",
    "text":      "text",
    "blob":      "bytea",
    "boolean":   "boolean",
    "datetime":  "timestamptz",
    "date":      "date",
    "":          "text",
}


def _pg_type(sqlite_type: str) -> str:
    t = (sqlite_type or "").strip().lower().split("(")[0]
    return _SQLITE_TO_PG.get(t, "text")


def _build_postgres_ddl(conn: sqlite3.Connection, table: str) -> str:
    col_info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    parts: list[str] = []
    pk_cols: list[str] = []
    for c in col_info:
        col_def = f'"{c["name"]}" {_pg_type(c["type"])}'
        if c["pk"]:
            pk_cols.append(c["name"])
            # auto-increment integer PK → serial / bigserial in PG
            if (c["type"] or "").lower().startswith("int") and len(col_info) > 0:
                col_def = f'"{c["name"]}" serial'
        if c["notnull"]:
            col_def += " NOT NULL"
        if c["dflt_value"] is not None:
            col_def += f' DEFAULT {c["dflt_value"]}'
        parts.append(col_def)
    if pk_cols:
        quoted = ", ".join('"' + p + '"' for p in pk_cols)
        parts.append(f'PRIMARY KEY ({quoted})')
    return f'CREATE TABLE IF NOT EXISTS "{table}" (\n  ' + ",\n  ".join(parts) + "\n);"


def _run_migration(
    job_id:        str,
    sqlite_path:   str,
    supabase_url:  str,
    supabase_key:  str,
):
    """Background task — copy SQLite schema + data to Supabase Postgres."""
    job = _MIGRATION_JOBS[job_id]
    job["status"]     = "running"
    job["started_at"] = time.time()

    try:
        # Lazy import: psycopg may not be on the path in some test envs.
        import psycopg
        from urllib.parse import urlparse, urlunparse

        # Supabase URL is the HTTP project URL; the Postgres connection string
        # lives in env (or is derived from the project ref). For v1 we expect
        # the caller to pass a real Postgres URL via supabase_url.
        pg_url = supabase_url

        src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        tables = [
            r["name"] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_\\_drizzle%' ESCAPE '\\'"
            ).fetchall()
        ]

        with psycopg.connect(pg_url) as dst:
            with dst.cursor() as cur:
                # 1. DDL — create each table in Postgres
                for i, t in enumerate(tables):
                    ddl = _build_postgres_ddl(src, t)
                    cur.execute(ddl)
                    job["progress"] = int(40 * (i + 1) / max(len(tables), 1))
                    job["message"]  = f"Created table {t}"

                # 2. Data — copy rows
                for i, t in enumerate(tables):
                    rows = src.execute(f'SELECT * FROM "{t}"').fetchall()
                    if not rows:
                        continue
                    cols = list(rows[0].keys())
                    placeholders = ", ".join(["%s"] * len(cols))
                    col_list = ", ".join(f'"{c}"' for c in cols)
                    cur.executemany(
                        f'INSERT INTO "{t}" ({col_list}) VALUES ({placeholders}) '
                        f'ON CONFLICT DO NOTHING',
                        [tuple(r) for r in rows],
                    )
                    job["progress"] = 40 + int(50 * (i + 1) / max(len(tables), 1))
                    job["message"]  = f"Copied {len(rows)} rows into {t}"
            dst.commit()

        src.close()
        job["status"]      = "succeeded"
        job["progress"]    = 100
        job["message"]     = f"Migrated {len(tables)} tables"
        job["finished_at"] = time.time()
    except Exception as exc:  # pragma: no cover — surface message to UI
        log.exception("migration failed")
        job["status"]      = "failed"
        job["message"]     = str(exc)
        job["finished_at"] = time.time()


# ── Provision (Phase A — Postgres-per-schema) ──────────────────────────────────
#
# `POST /api/projects/{id}/db/provision` creates a Postgres schema + role
# inside Forge's own Postgres for the project's generated app. This is the
# `local-self-host` mode default; in `hosted` mode it refuses and the
# supabase.md skill walks the user through BYO Supabase OAuth instead.
#
# The endpoint is idempotent: if a provisioned-local row already exists for
# the project, it returns it without re-running DDL. If a BYO Supabase row
# exists, it refuses (409 — disconnect first to switch modes).
#
# Phase A only wires creation. Phase B will:
#   - rewrite forge-bootstrap.sh to call this on first AI DB request
#   - rewrite /db/tables, /db/rows etc. to query the schema via asyncpg
#   - inject the connection string into the runner container env
#
# Network reachability for the runner container is a Phase B concern — the
# connection string returned here points at Forge's Postgres host as-seen-
# from-the-host. From inside a container the URL host needs translation
# (host.docker.internal on macOS, host-gateway on Linux). For now we return
# the literal URL and document it.


class ProvisionResponse(BaseModel):
    provisioned:           bool
    schema_name:           str
    role_name:             str
    # The connection string includes a freshly-generated role password. Treat
    # as a secret: the AI must write this to .env and never echo to chat. The
    # supabase.md skill will be updated in Phase B with that guidance.
    database_url:          str
    # True if this call did the DDL; False if the row already existed and we
    # returned cached state (idempotent path).
    created:               bool


def _provisioned_schema(project_id: str) -> str:
    """
    Postgres identifier for the project's schema. UUID hex (no dashes) is
    safe for identifiers (all [0-9a-f]); the `app_` prefix guarantees a
    letter start and avoids collisions with Postgres-internal schemas.

    First 8 chars of the UUID gives 32 bits of distinctness — at 100k
    projects per Forge instance the birthday-collision probability is
    ~0.001 (negligible for OSS scale, recheck if we ever hit hosted).
    """
    return f"app_{project_id.replace('-', '')[:8]}"


def _runner_database_url(role: str, password: str, schema: str) -> str:
    """
    Build the Postgres connection string the runner container will use.

    `options=-csearch_path%3D<schema>` makes every query default to the
    project's schema without the generated app having to qualify names —
    keeps the Drizzle scaffolding clean. URL-encoded `=` as `%3D` is
    required by libpq for options.

    Host is whatever Forge itself uses — fine for the host process; runner
    containers will need host-gateway translation (Phase B).
    """
    # Strip SQLAlchemy's "+asyncpg" suffix; the runner uses node-postgres.
    base = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    # Replace the credentials segment with the new role's credentials.
    # base looks like "postgresql://USER:PW@HOST:PORT/DB"
    after_proto = base.split("://", 1)[1]
    host_and_db = after_proto.split("@", 1)[1] if "@" in after_proto else after_proto
    return f"postgresql://{role}:{password}@{host_and_db}?options=-csearch_path%3D{schema}"


@router.post("/{project_id}/db/provision", response_model=ProvisionResponse)
async def provision_local_db(
    project_id: str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Create a Postgres schema + role for this project inside Forge's local
    Postgres. Local-self-host mode only.
    """
    # Mode guard — hosted Forge MUST use BYO Supabase; refuse loudly.
    if settings.forge_mode != "local-self-host":
        raise HTTPException(
            409,
            f"db/provision is disabled in mode={settings.forge_mode!r}. "
            "Use the BYO Supabase flow (/api/supabase/connect).",
        )

    project = await _get_owned_project(project_id, user, db)

    # Idempotency — if a row already exists, branch on its type.
    existing = (await db.execute(
        select(SupabaseConnection).where(SupabaseConnection.project_id == project_id)
    )).scalar_one_or_none()

    if existing is not None:
        if not existing.provisioned_locally:
            raise HTTPException(
                409,
                "Project already has a BYO Supabase connection. Disconnect it "
                "first (/api/supabase/disconnect) to switch to provisioned-local.",
            )
        # Same project, second call → return existing without re-running DDL.
        # Decrypt the stored password to rebuild the URL.
        from forge_server.api.supabase_routes import _fernet   # reuse — same key derivation
        pw = _fernet().decrypt((existing.role_password_enc or "").encode()).decode()
        return ProvisionResponse(
            provisioned=True,
            schema_name=existing.schema_name or "",
            role_name=existing.role_name or "",
            database_url=_runner_database_url(existing.role_name or "", pw, existing.schema_name or ""),
            created=False,
        )

    # Fresh provision.
    import secrets
    from sqlalchemy import text
    from forge_server.api.supabase_routes import _fernet

    schema_name = _provisioned_schema(project_id)
    role_name   = schema_name                    # same identifier — one per project
    role_password = secrets.token_urlsafe(32)    # ~256 bits of entropy

    # DDL — separate statements. CREATE SCHEMA IF NOT EXISTS is safe-by-default
    # (no error if reattempted). CREATE ROLE has no IF NOT EXISTS until PG 16,
    # so we check pg_roles first to stay compatible with older Postgres in
    # the wild (Supabase local pins PG 15 currently).
    await db.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))

    role_exists = (await db.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": role_name}
    )).scalar_one_or_none()
    if role_exists is None:
        # asyncpg/SQLA can't bind into CREATE ROLE's PASSWORD clause directly.
        # secrets.token_urlsafe() returns only [A-Za-z0-9_-], so a single-quoted
        # SQL literal is safe (no quote chars in the token, no injection vector).
        await db.execute(text(f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD '{role_password}'"))
    else:
        # Role exists from a prior failed run — rotate the password so the
        # connstr we return is valid. Same safety argument on the literal.
        await db.execute(text(f"ALTER ROLE \"{role_name}\" PASSWORD '{role_password}'"))

    # Grants — read+write on this schema only (user is building their app).
    # ALTER DEFAULT PRIVILEGES catches tables the role itself creates later.
    await db.execute(text(
        f'GRANT USAGE, CREATE ON SCHEMA "{schema_name}" TO "{role_name}"'
    ))
    await db.execute(text(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema_name}" '
        f'GRANT ALL PRIVILEGES ON TABLES    TO "{role_name}"'
    ))
    await db.execute(text(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema_name}" '
        f'GRANT ALL PRIVILEGES ON SEQUENCES TO "{role_name}"'
    ))

    # Persist. Encrypt the password — never store plaintext at rest.
    row = SupabaseConnection(
        project_id          = project_id,
        # supabase_url + anon_key are NOT NULL columns; fill with the
        # host-facing URL and the schema name as a non-secret label. The
        # real connstr is rebuilt on read from role_name + decrypted pw.
        supabase_url        = settings.database_url.split("@", 1)[-1],   # host:port/db, no creds
        anon_key            = schema_name,                               # label only
        service_role_key    = None,
        provisioned_locally = True,
        schema_name         = schema_name,
        role_name           = role_name,
        role_password_enc   = _fernet().encrypt(role_password.encode()).decode(),
    )
    db.add(row)
    await db.commit()

    return ProvisionResponse(
        provisioned=True,
        schema_name=schema_name,
        role_name=role_name,
        database_url=_runner_database_url(role_name, role_password, schema_name),
        created=True,
    )


class MigrateRequest(BaseModel):
    postgres_url: str | None = None   # if omitted, use stored Supabase connection


@router.post("/{project_id}/db/migrate-to-supabase", response_model=MigrateJob)
async def start_migration(
    project_id: str,
    body:       MigrateRequest,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(project_id, user, db)

    pg_url = body.postgres_url
    if not pg_url:
        # Optional convenience: pull from an env var the user set via the
        # platform env-vars API (DATABASE_URL is the convention).
        from forge_server.db.models import ProjectEnvVar
        from forge_server.api.config_routes import _decrypt  # type: ignore
        r = await db.execute(
            select(ProjectEnvVar).where(
                ProjectEnvVar.project_id == project_id,
                ProjectEnvVar.key.in_(["DATABASE_URL", "SUPABASE_DB_URL", "POSTGRES_URL"]),
            )
        )
        row = r.scalars().first()
        if row is None:
            raise HTTPException(
                400,
                "No Postgres URL — pass postgres_url in the request body, "
                "or set DATABASE_URL via the project env vars first.",
            )
        try:
            pg_url = _decrypt(row.value_enc)
        except Exception:
            raise HTTPException(500, "Failed to read stored DATABASE_URL")

    job_id = uuid.uuid4().hex
    _MIGRATION_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress": 0,
        "message": "Queued",
        "started_at": None,
        "finished_at": None,
    }

    background.add_task(
        _run_migration,
        job_id,
        str(_db_path(project)),
        pg_url,
        "",  # service-role key (reserved for future RLS bypass)
    )
    return MigrateJob(**_MIGRATION_JOBS[job_id])


@router.get("/{project_id}/db/migrate-to-supabase/{job_id}", response_model=MigrateJob)
async def migration_status(
    project_id: str,
    job_id:     str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    await _get_owned_project(project_id, user, db)
    job = _MIGRATION_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return MigrateJob(**job)
