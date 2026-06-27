"""
forge_server/api/runtime_errors_routes.py
==========================================
HTTP surface for the runtime-errors ring.

Endpoints (all under /api/projects/{project_id}/runtime-errors):

  POST   ↦ ingest one error. Callers:
           - in-iframe bridge in the user's app (forwarded by forge-ui)
           - agent-side tooling that wants to log its own findings
           Body is loosely-typed by design — the iframe ships whatever it
           can scrape from window.onerror / unhandledrejection / fetch
           wrappers, and we accept it as-is (subject to size limits).

  GET    ↦ read the ring, newest first. Optional `since` (epoch seconds)
           filters to errors strictly after that timestamp — the agent
           polls with the timestamp of the last error it saw to avoid
           re-processing duplicates.

  DELETE ↦ clear the ring for a project. Called by the agent after it
           addresses the errors, so the next read returns "ok / nothing
           pending" rather than the stale list.

Auth model:
  - GET and DELETE require the project owner (matches existing pattern
    in verify_routes.py).
  - POST accepts the project owner OR an unauthenticated request from
    the iframe bridge. The iframe is third-party JS executing in the
    user's preview; it can't reasonably carry the user's JWT. We mitigate
    by:
      * matching the Origin header to the project's expected preview URL
      * size-capping the payload aggressively
      * rate-limiting via the dedup window in runtime_errors_store
    This is "trust the browser to only post things it saw" — not a
    secret, just runtime-error breadcrumbs.
"""
from __future__ import annotations

import base64
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user_optional
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import Project, User
from forge_server.runner.runtime_errors_store import (
    record as store_record,
    list_errors as store_list,
    clear as store_clear,
)

log      = logging.getLogger("forge.runtime_errors_api")
settings = get_settings()
router   = APIRouter(prefix="/api/projects", tags=["runtime-errors"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class RuntimeErrorIn(BaseModel):
    """
    What the bridge / agent posts. All fields optional except `message`;
    the iframe can't always know the file/line/stack.
    """
    source:    str | None = Field(default="browser", max_length=24)
    signature: str | None = Field(default=None,     max_length=64)
    detail:    str | None = Field(default=None,     max_length=300)
    message:   str        = Field(...,              max_length=500)
    file:      str | None = Field(default=None,     max_length=400)
    line:      int | None = None
    column:    int | None = None
    stack:     str | None = Field(default=None,     max_length=4000)
    url:       str | None = Field(default=None,     max_length=600)
    status:    int | None = None
    user_agent: str | None = Field(default=None,    max_length=300)


class RuntimeErrorOut(BaseModel):
    fingerprint: str
    ts:          float
    source:      str
    signature:   str | None = None
    detail:      str | None = None
    message:     str | None = None
    file:        str | None = None
    line:        int | None = None
    stack:       str | None = None
    url:         str | None = None
    status:      int | None = None


class IngestAck(BaseModel):
    stored: bool


class ClearAck(BaseModel):
    cleared: int


# ── Helpers ──────────────────────────────────────────────────────────────────

# The FE identifies projects in some routes by the SolidJS `dir` route param,
# which is the base64-encoded workspace directory path (opencode upstream
# identifies projects by directory, Forge by UUID — this is the seam). The
# decoded path always looks like:
#   /…/forge-data/users/<USER_UUID>/projects/<PROJECT_UUID>/workspace
# so we can pull the project UUID out of it without a schema change or an
# extra DB lookup. Anything that already looks like a UUID passes through
# unchanged.
_UUID_RE       = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_PROJ_PATH_RE  = re.compile(r"/projects/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)", re.I)


def _normalize_project_id(raw: str) -> str:
    """Accept a UUID, or a base64-encoded workspace path that embeds one."""
    if _UUID_RE.match(raw):
        return raw
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.b64decode(padded, validate=False).decode("utf-8", errors="ignore")
    except Exception:
        # Fall through — the UUID lookup will produce the original 422/500
        # surface, which is still better than masking with a 404.
        return raw
    match = _PROJ_PATH_RE.search(decoded)
    return match.group(1) if match else raw


async def _project_exists(project_id: str, db: AsyncSession) -> Project:
    project_id = _normalize_project_id(project_id)
    p = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Project not found")
    return p


async def _owned(project_id: str, user: User | None, db: AsyncSession) -> Project:
    p = await _project_exists(project_id, db)
    if user is None or p.user_id != user.id:
        raise HTTPException(403, "Forbidden")
    return p


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/{project_id}/runtime-errors", response_model=IngestAck)
async def ingest(
    project_id: str,
    payload:    RuntimeErrorIn,
    request:    Request,
    user:       User | None    = Depends(current_user_optional),
    db:         AsyncSession   = Depends(get_db),
):
    """
    Accept one error. Open to unauthenticated POSTs IF the Origin header
    matches this project's preview URL — anything else requires the owner.
    """
    # Normalize once so DB lookup, Origin check, and ring key all share the
    # same canonical UUID even when the FE passed a base64 dir path.
    project_id = _normalize_project_id(project_id)
    project = await _project_exists(project_id, db)

    if user is None or project.user_id != user.id:
        origin = (request.headers.get("origin") or "").lower()
        expected_host = f"{project_id}.{settings.preview_domain}".lower()
        if expected_host not in origin:
            raise HTTPException(403, "Forbidden")

    stored = await store_record(project_id, payload.model_dump(exclude_none=True))
    return IngestAck(stored=stored)


@router.get("/{project_id}/runtime-errors", response_model=list[RuntimeErrorOut])
async def list_runtime_errors(
    project_id: str,
    since:      float | None = None,
    user:       User | None  = Depends(current_user_optional),
    db:         AsyncSession = Depends(get_db),
):
    """Newest-first list. `since` (epoch seconds) returns only newer entries."""
    project_id = _normalize_project_id(project_id)
    await _owned(project_id, user, db)
    items = await store_list(project_id, since_ts=since)
    return [RuntimeErrorOut(**i) for i in items]


@router.delete("/{project_id}/runtime-errors", response_model=ClearAck)
async def clear_runtime_errors(
    project_id: str,
    user:       User | None  = Depends(current_user_optional),
    db:         AsyncSession = Depends(get_db),
):
    """Drop everything for a project. Called by the agent after addressing."""
    project_id = _normalize_project_id(project_id)
    await _owned(project_id, user, db)
    n = await store_clear(project_id)
    return ClearAck(cleared=n)
