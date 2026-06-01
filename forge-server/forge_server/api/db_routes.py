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


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/{project_id}/db/info")
async def db_info(
    project_id: str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(project_id, user, db)
    p = _db_path(project)
    return {
        "driver": "sqlite",
        "path":   "data.db",
        "exists": p.exists(),
        "size_bytes": p.stat().st_size if p.exists() else 0,
    }


@router.get("/{project_id}/db/tables", response_model=TablesResponse)
async def list_tables(
    project_id: str,
    include_internal: bool = Query(False),
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    project = await _get_owned_project(project_id, user, db)
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
    is_write = first_word in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}
    if is_write and not write:
        raise HTTPException(403, "Write statements require ?write=1")

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
