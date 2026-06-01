"""
forge_server/api/supabase_routes.py
=====================================
Supabase integration — everything Lovable does:
  - Connect a Supabase project (store credentials)
  - Auto-inject credentials into workspace .env
  - List tables
  - Browse table rows (paginated)
  - Run SQL queries
  - Disconnect

The service_role_key is stored encrypted using Fernet symmetric encryption
so it's safe at rest in the database.
"""
from __future__ import annotations

import logging
import os
from base64 import urlsafe_b64encode
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user
# forge.json is gone — supabase connection state lives in the
# supabase_connections table and is read via GET /api/projects/{id}/config.
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import Project, SupabaseConnection, User

log      = logging.getLogger("forge.supabase")
settings = get_settings()
router   = APIRouter(prefix="/api/supabase", tags=["supabase"])


# ── Encryption helpers ────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    """
    Derive a Fernet key from jwt_secret.
    In production, set SUPABASE_ENCRYPT_KEY env var to a proper 32-byte key.
    """
    raw = os.environ.get("SUPABASE_ENCRYPT_KEY")
    if raw:
        key = raw.encode()[:32].ljust(32, b"0")
    else:
        key = settings.jwt_secret.encode()[:32].ljust(32, b"0")
    return Fernet(urlsafe_b64encode(key))


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


# ── .env injection ────────────────────────────────────────────────────────────

def _inject_env(workspace_path: str, url: str, anon_key: str, service_role_key: str | None) -> None:
    """
    Write / update NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY
    in the workspace's .env and .env.local files.

    Also writes SUPABASE_SERVICE_ROLE_KEY if provided (for server-side usage).
    The AI-generated code can import these directly via process.env.
    """
    ws = Path(workspace_path)
    lines_to_add = {
        "NEXT_PUBLIC_SUPABASE_URL":      url,
        "NEXT_PUBLIC_SUPABASE_ANON_KEY": anon_key,
        "SUPABASE_URL":                  url,
        "SUPABASE_ANON_KEY":             anon_key,
        "VITE_SUPABASE_URL":             url,
        "VITE_SUPABASE_ANON_KEY":        anon_key,
    }
    if service_role_key:
        lines_to_add["SUPABASE_SERVICE_ROLE_KEY"] = service_role_key

    for env_file in (".env", ".env.local"):
        path = ws / env_file
        existing: dict[str, str] = {}

        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip()

        existing.update(lines_to_add)

        content = "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n"
        path.write_text(content)

    log.info("Injected Supabase env into workspace %s", workspace_path)


# ── Supabase REST helpers ─────────────────────────────────────────────────────

async def _supa_get(url: str, key: str, path: str, params: dict | None = None) -> Any:
    """GET {url}/rest/v1/{path} using the service role key."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{url.rstrip('/')}/rest/v1/{path.lstrip('/')}",
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
            },
            params=params,
        )
        r.raise_for_status()
        return r.json()


async def _supa_post(url: str, key: str, sql: str) -> Any:
    """POST {url}/rest/v1/rpc/query — run raw SQL via the pg REST endpoint."""
    # Supabase exposes pg_meta for schema inspection and /rest/v1 for data.
    # For raw SQL we use the Management API or pg-meta.
    # Simpler: use the REST API to query information_schema.
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{url.rstrip('/')}/rest/v1/rpc/exec_sql",
            headers={
                "apikey":         key,
                "Authorization":  f"Bearer {key}",
                "Content-Type":   "application/json",
                "Prefer":         "return=representation",
            },
            json={"query": sql},
        )
        if r.status_code == 404:
            # exec_sql not available — fall back to pg-meta
            raise HTTPException(
                status_code=400,
                detail="Raw SQL requires the exec_sql RPC function in your Supabase project. "
                       "See: https://supabase.com/docs/guides/database/functions",
            )
        r.raise_for_status()
        return r.json()


async def _list_tables(url: str, key: str) -> list[dict]:
    """List all user tables via Supabase's pg-meta endpoint."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{url.rstrip('/')}/pg-meta/v1/tables",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={"limit": 200, "include_system_schemas": "false"},
        )
        if r.status_code in (401, 403):
            raise HTTPException(status_code=401, detail="Invalid Supabase service role key")
        r.raise_for_status()
        return r.json()


# ── Schemas ───────────────────────────────────────────────────────────────────

class ConnectIn(BaseModel):
    project_id:       str
    supabase_url:     str
    anon_key:         str
    service_role_key: str | None = None


class ConnectOut(BaseModel):
    project_id:   str
    supabase_url: str
    connected_at: datetime


class TableRow(BaseModel):
    name:       str
    schema:     str
    row_count:  int | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

async def _get_project(project_id: str, user: User, db: AsyncSession) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


async def _get_connection(project_id: str, db: AsyncSession) -> SupabaseConnection:
    result = await db.execute(
        select(SupabaseConnection).where(SupabaseConnection.project_id == project_id)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="No Supabase connection for this project")
    return conn


@router.post("/connect", response_model=ConnectOut)
async def connect(
    body: ConnectIn,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Store Supabase credentials and inject them into the workspace .env."""
    project = await _get_project(body.project_id, user, db)

    # Upsert connection
    result = await db.execute(
        select(SupabaseConnection).where(SupabaseConnection.project_id == body.project_id)
    )
    conn = result.scalar_one_or_none()

    encrypted_srk = _encrypt(body.service_role_key) if body.service_role_key else None

    if conn:
        conn.supabase_url     = body.supabase_url
        conn.anon_key         = body.anon_key
        conn.service_role_key = encrypted_srk
        conn.last_used_at     = datetime.utcnow()
    else:
        conn = SupabaseConnection(
            project_id       = body.project_id,
            supabase_url     = body.supabase_url,
            anon_key         = body.anon_key,
            service_role_key = encrypted_srk,
        )
        db.add(conn)

    await db.commit()
    await db.refresh(conn)

    # Inject into workspace .env
    _inject_env(
        project.workspace_path,
        body.supabase_url,
        body.anon_key,
        body.service_role_key,
    )

    # No forge.json sync — supabase connection state is read from the
    # supabase_connections row we just committed (see /api/projects/{id}/config).

    return ConnectOut(
        project_id   = body.project_id,
        supabase_url = body.supabase_url,
        connected_at = conn.connected_at,
    )


@router.get("/status")
async def status(
    project_id: str        = Query(...),
    user:       User       = Depends(current_user),
    db:         AsyncSession = Depends(get_db),
):
    await _get_project(project_id, user, db)
    result = await db.execute(
        select(SupabaseConnection).where(SupabaseConnection.project_id == project_id)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        return {"connected": False}
    return {
        "connected":    True,
        "supabase_url": conn.supabase_url,
        "connected_at": conn.connected_at,
        "last_used_at": conn.last_used_at,
    }


@router.get("/tables")
async def list_tables(
    project_id: str        = Query(...),
    user:       User       = Depends(current_user),
    db:         AsyncSession = Depends(get_db),
):
    await _get_project(project_id, user, db)
    conn = await _get_connection(project_id, db)
    key  = _decrypt(conn.service_role_key) if conn.service_role_key else conn.anon_key

    try:
        tables = await _list_tables(conn.supabase_url, key)
        conn.last_used_at = datetime.utcnow()
        await db.commit()
        return {"tables": tables}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Supabase error: {exc}")


@router.get("/table/{table_name}")
async def get_table_rows(
    table_name: str,
    project_id: str        = Query(...),
    limit:      int        = Query(50,  ge=1, le=500),
    offset:     int        = Query(0,   ge=0),
    user:       User       = Depends(current_user),
    db:         AsyncSession = Depends(get_db),
):
    """Fetch rows from a table using the Supabase REST API."""
    await _get_project(project_id, user, db)
    conn = await _get_connection(project_id, db)
    key  = _decrypt(conn.service_role_key) if conn.service_role_key else conn.anon_key

    try:
        rows = await _supa_get(
            conn.supabase_url, key, table_name,
            params={"limit": limit, "offset": offset},
        )
        conn.last_used_at = datetime.utcnow()
        await db.commit()
        return {"table": table_name, "rows": rows, "offset": offset, "limit": limit}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Supabase error: {exc}")


@router.post("/query")
async def run_query(
    project_id: str        = Query(...),
    body:       dict       = ...,
    user:       User       = Depends(current_user),
    db:         AsyncSession = Depends(get_db),
):
    """Run a raw SQL query via Supabase's exec_sql RPC."""
    sql = (body or {}).get("sql", "").strip()
    if not sql:
        raise HTTPException(status_code=400, detail="sql is required")

    await _get_project(project_id, user, db)
    conn = await _get_connection(project_id, db)
    key  = _decrypt(conn.service_role_key) if conn.service_role_key else conn.anon_key

    try:
        result = await _supa_post(conn.supabase_url, key, sql)
        conn.last_used_at = datetime.utcnow()
        await db.commit()
        return {"result": result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Query error: {exc}")


@router.delete("/disconnect")
async def disconnect(
    project_id: str        = Query(...),
    user:       User       = Depends(current_user),
    db:         AsyncSession = Depends(get_db),
):
    project = await _get_project(project_id, user, db)
    result  = await db.execute(
        select(SupabaseConnection).where(SupabaseConnection.project_id == project_id)
    )
    conn = result.scalar_one_or_none()
    if conn:
        await db.delete(conn)
        await db.commit()

    # No forge.json sync — disconnection is reflected by the absence of a
    # supabase_connections row, which /api/projects/{id}/config surfaces.

    return {"disconnected": True}
