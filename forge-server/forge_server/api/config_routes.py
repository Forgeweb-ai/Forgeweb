"""
forge_server/api/config_routes.py
===================================
Per-project configuration API.

Endpoints:
  GET    /api/projects/{id}/config         — read project config (DB only, no secrets)
  PATCH  /api/projects/{id}/config         — update project fields (name, stack, services)
  GET    /api/projects/{id}/env            — list env var keys (values are masked)
  POST   /api/projects/{id}/env            — add or update an env var (value encrypted in DB)
  DELETE /api/projects/{id}/env/{key}      — remove an env var

Design:
  - Project config used to live in workspace/forge.json. That file is GONE —
    the database is now the single source of truth. The shape returned by
    GET /config matches what forge.json used to look like, so the AI skills
    and UI carried no behavior changes when the file was removed.
  - Actual secret values live in project_env_vars (encrypted with Fernet).
  - On every env var write, the workspace .env files are re-injected so
    the dev container picks up changes on next start/restart. (The .env
    files belong to the user's app — those stay on disk.)
"""
from __future__ import annotations

import json
import logging
import os
from base64 import urlsafe_b64encode
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import Project, ProjectEnvVar, SupabaseConnection, User

log      = logging.getLogger("forge.config")
settings = get_settings()
router   = APIRouter(prefix="/api/projects", tags=["config"])


# ── Encryption (shared with supabase_routes) ──────────────────────────────────

def _fernet() -> Fernet:
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


# ── .env file injection (workspace-side, app runtime only) ───────────────────

def _inject_env_files(workspace_path: str, env_vars: list[ProjectEnvVar]) -> None:
    """
    Re-write .env and .env.local in the workspace with current env var values.
    Existing keys not managed by forge are preserved.

    These .env files belong to the user's running app — they're not Forge
    metadata. The workspace stays user-owned; this is the one exception, and
    only because frameworks read from .env at boot.
    """
    ws = Path(workspace_path)
    managed: dict[str, str] = {}
    for ev in env_vars:
        try:
            managed[ev.key] = _decrypt(ev.value_enc)
        except Exception:
            log.warning("Could not decrypt env var %s", ev.key)

    for env_file in (".env", ".env.local"):
        path = ws / env_file
        existing: dict[str, str] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip()

        existing.update(managed)
        content = "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n"
        path.write_text(content)

    log.info("Re-injected %d env vars into workspace %s", len(managed), workspace_path)


# ── Auth helper ───────────────────────────────────────────────────────────────

async def _get_project(project_id: str, user: User, db: AsyncSession) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


def _services_default() -> dict:
    return {
        "frontend": {"framework": None, "port": 3000},
        "backend":  {"framework": None, "port": 8000},
    }


async def _build_config_payload(project: Project, db: AsyncSession) -> dict:
    """Assemble the response shape skills + UI expect (formerly forge.json)."""
    # Supabase block
    sup_row = (
        await db.execute(
            select(SupabaseConnection).where(SupabaseConnection.project_id == project.id)
        )
    ).scalar_one_or_none()
    supabase_block = {
        "connected":    sup_row is not None,
        "url":          sup_row.supabase_url if sup_row else None,
        "connected_at": sup_row.connected_at.isoformat() + "Z" if sup_row else None,
    }

    # Env var metadata (keys + labels only; never values)
    ev_rows = (
        await db.execute(
            select(ProjectEnvVar)
            .where(ProjectEnvVar.project_id == project.id)
            .order_by(ProjectEnvVar.key)
        )
    ).scalars().all()
    env_vars = [
        {
            "key":            ev.key,
            "label":          ev.label,
            "inject_runtime": ev.inject_runtime,
            "set_at":         ev.created_at.isoformat() + "Z",
        }
        for ev in ev_rows
    ]

    # Services block (decoded from services_json, with sane default)
    services: dict
    if project.services_json:
        try:
            services = json.loads(project.services_json)
        except json.JSONDecodeError:
            log.warning("services_json corrupted for project %s — using default", project.id)
            services = _services_default()
    else:
        services = _services_default()

    return {
        "version":             1,
        "project_id":          project.id,
        "name":                project.name,
        "description":         project.description or "",
        "stack":               project.stack,
        "opencode_session_id": project.opencode_session_id,
        "supabase":            supabase_block,
        "env_vars":            env_vars,
        "services":            services,
        "created_at":          project.created_at.isoformat() + "Z",
        "updated_at":          project.updated_at.isoformat() + "Z",
    }


# ── Schemas ───────────────────────────────────────────────────────────────────

class ConfigPatch(BaseModel):
    """Fields the AI or UI can update."""
    name:                str | None = None
    description:         str | None = None
    stack:               str | None = None
    opencode_session_id: str | None = None
    services:            dict | None = None


class EnvVarIn(BaseModel):
    key:            str
    value:          str
    label:          str | None = None
    inject_runtime: bool = True


class EnvVarOut(BaseModel):
    key:            str
    label:          str | None = None
    inject_runtime: bool
    set_at:         datetime


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/{project_id}/config")
async def get_config(
    project_id: str,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Return the project's current config — built entirely from the database.
    Safe to call from UI and from AI (no secret values included).
    """
    project = await _get_project(project_id, user, db)
    return await _build_config_payload(project, db)


@router.patch("/{project_id}/config")
async def patch_config(
    project_id: str,
    body: ConfigPatch,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Update project config — callable by AI via bash (forge-platform skill)
    or by the UI settings panel. All writes go to the DB; nothing touches
    the workspace.
    """
    project = await _get_project(project_id, user, db)

    if body.name is not None:
        project.name = body.name
    if body.description is not None:
        project.description = body.description
    if body.stack is not None:
        project.stack = body.stack
    if body.opencode_session_id is not None:
        project.opencode_session_id = body.opencode_session_id
    if body.services is not None:
        # Merge with whatever is currently stored so a partial patch (e.g.
        # only setting frontend.framework) doesn't blow away the other half.
        current = _services_default()
        if project.services_json:
            try:
                current = json.loads(project.services_json)
            except json.JSONDecodeError:
                pass
        # Shallow merge at the top level (frontend/backend); deep at one level.
        for key, value in body.services.items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                current[key] = {**current[key], **value}
            else:
                current[key] = value
        project.services_json = json.dumps(current)

    await db.commit()
    await db.refresh(project)

    return {"ok": True, "updated": body.model_dump(exclude_none=True)}


# ── Env var routes ────────────────────────────────────────────────────────────

@router.get("/{project_id}/env", response_model=list[EnvVarOut])
async def list_env_vars(
    project_id: str,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """List env var keys for this project. Values are never returned."""
    await _get_project(project_id, user, db)
    result = await db.execute(
        select(ProjectEnvVar)
        .where(ProjectEnvVar.project_id == project_id)
        .order_by(ProjectEnvVar.key)
    )
    evs = result.scalars().all()
    return [
        EnvVarOut(
            key=ev.key, label=ev.label,
            inject_runtime=ev.inject_runtime, set_at=ev.created_at,
        )
        for ev in evs
    ]


@router.post("/{project_id}/env", response_model=EnvVarOut, status_code=201)
async def set_env_var(
    project_id: str,
    body: EnvVarIn,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Add or update an env var. Value is encrypted before storing.
    Also re-injects all env vars into the workspace .env files so the
    dev container picks them up on next start.
    """
    project = await _get_project(project_id, user, db)

    # Validate key name
    key = body.key.strip().upper()
    if not key or not key.replace("_", "").isalnum():
        raise HTTPException(status_code=422, detail="Invalid env var key name")

    encrypted = _encrypt(body.value)

    # Upsert
    result = await db.execute(
        select(ProjectEnvVar).where(
            ProjectEnvVar.project_id == project_id,
            ProjectEnvVar.key == key,
        )
    )
    ev = result.scalar_one_or_none()
    if ev:
        ev.value_enc     = encrypted
        ev.label         = body.label
        ev.inject_runtime = body.inject_runtime
        ev.updated_at    = datetime.utcnow()
    else:
        ev = ProjectEnvVar(
            project_id     = project_id,
            key            = key,
            value_enc      = encrypted,
            label          = body.label,
            inject_runtime = body.inject_runtime,
        )
        db.add(ev)

    await db.flush()

    # Re-inject all env vars into .env files (workspace-side runtime config —
    # NOT a forge.json write; this is what the user's app actually reads).
    all_evs_result = await db.execute(
        select(ProjectEnvVar).where(ProjectEnvVar.project_id == project_id)
    )
    all_evs = all_evs_result.scalars().all()
    _inject_env_files(project.workspace_path, list(all_evs))

    await db.commit()
    await db.refresh(ev)
    return EnvVarOut(
        key=ev.key, label=ev.label,
        inject_runtime=ev.inject_runtime, set_at=ev.created_at,
    )


@router.delete("/{project_id}/env/{key}", status_code=204)
async def delete_env_var(
    project_id: str,
    key:        str,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Remove an env var and re-inject remaining vars into .env files."""
    project = await _get_project(project_id, user, db)
    key = key.strip().upper()

    result = await db.execute(
        select(ProjectEnvVar).where(
            ProjectEnvVar.project_id == project_id,
            ProjectEnvVar.key == key,
        )
    )
    ev = result.scalar_one_or_none()
    if not ev:
        raise HTTPException(status_code=404, detail=f"Env var '{key}' not found")

    await db.delete(ev)
    await db.flush()

    # Re-inject remaining vars
    all_evs_result = await db.execute(
        select(ProjectEnvVar).where(ProjectEnvVar.project_id == project_id)
    )
    all_evs = all_evs_result.scalars().all()
    _inject_env_files(project.workspace_path, list(all_evs))

    await db.commit()
