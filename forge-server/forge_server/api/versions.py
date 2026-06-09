"""
forge_server/api/versions.py
============================
Per-AI-turn version history for a project.

Routes
──────
  GET  /api/projects/{id}/versions               → list (newest first)
  POST /api/projects/{id}/versions               → snapshot current workspace
  POST /api/projects/{id}/versions/{vid}/restore → soft rollback

The POST snapshot route is currently a manual trigger (callable by the
post-verify hook from the AI-turn flow). It is also useful for the FE
"save now" affordance. The legacy background tarball-snapshot worker is
NOT replaced by this yet — both run side-by-side until the AI-turn hook
ships and the legacy worker can be retired (see follow-ups).

Per CLAUDE.md §4: every endpoint is owner-checked, body-validated, and
returns a stable response shape so the FE doesn't break on field renames.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user
from forge_server.db.database import get_db
from forge_server.db.models import Project, User
from forge_server.storage.versions import (
    create_version,
    list_versions,
    restore_version,
)

router = APIRouter(prefix="/api/projects", tags=["versions"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class VersionOut(BaseModel):
    id:                str
    parent_version_id: Optional[str] = None
    prompt:            Optional[str] = None
    summary:           Optional[str] = None
    created_at:        datetime
    # `is_head` lets the FE highlight the currently-materialized version
    # without a separate request. Computed at response time from
    # project.head_version_id.
    is_head:           bool = False
    # Non-null when this version was soft-deleted by a rollback. The
    # default list excludes orphans, so this is normally false in the
    # standard dropdown query.
    orphaned_at:       Optional[datetime] = None


class VersionListOut(BaseModel):
    head_version_id: Optional[str] = None
    versions:        list[VersionOut]


class CreateVersionIn(BaseModel):
    # `prompt` and `summary` are optional — the FE "save now" flow leaves
    # them empty; the AI-turn hook fills them with the user prompt and
    # an AI-generated label.
    prompt:  Optional[str] = Field(default=None, max_length=8000)
    summary: Optional[str] = Field(default=None, max_length=200)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _owned_project(
    project_id: str, user: User, db: AsyncSession,
) -> Project:
    """Fetch a project the caller owns, or 404. Centralised so every route
    in this module has the same ownership story."""
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{project_id}/versions", response_model=VersionListOut)
async def list_project_versions(
    project_id: str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
) -> VersionListOut:
    """List live (non-orphaned) versions newest-first. Drives the dropdown."""
    project = await _owned_project(project_id, user, db)
    versions = await list_versions(db, str(project.id))
    head_id  = str(project.head_version_id) if project.head_version_id else None
    return VersionListOut(
        head_version_id=head_id,
        versions=[
            VersionOut(
                id                = str(v.id),
                parent_version_id = str(v.parent_version_id) if v.parent_version_id else None,
                prompt            = v.prompt,
                summary           = v.summary,
                created_at        = v.created_at,
                is_head           = (str(v.id) == head_id),
                orphaned_at       = v.orphaned_at,
            )
            for v in versions
        ],
    )


@router.post("/{project_id}/versions", response_model=VersionOut, status_code=201)
async def create_project_version(
    project_id: str,
    body: CreateVersionIn,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
) -> VersionOut:
    """Snapshot the current workspace as a new version.

    Idempotency note: calling this twice in a row with no workspace
    changes creates two versions with identical manifests. That's fine
    — they share blob refs (zero extra storage) and the second is
    cheap (one DB row + one refcount UPDATE). Dedup at the call site
    if you care about identical-version noise.
    """
    project = await _owned_project(project_id, user, db)
    try:
        v = await create_version(db, project, prompt=body.prompt, summary=body.summary)
    except RuntimeError as e:
        # Empty workspace — surface as 409, not 500.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        # File-too-large guard from versions._hash_file_sync.
        raise HTTPException(status_code=413, detail=str(e))
    return VersionOut(
        id                = str(v.id),
        parent_version_id = str(v.parent_version_id) if v.parent_version_id else None,
        prompt            = v.prompt,
        summary           = v.summary,
        created_at        = v.created_at,
        is_head           = True,
        orphaned_at       = None,
    )


@router.post("/{project_id}/versions/{version_id}/restore", response_model=VersionOut)
async def restore_project_version(
    project_id: str,
    version_id: str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
) -> VersionOut:
    """Soft-rollback the workspace to `version_id`.

    Descendants of the target are marked orphaned but kept until GC.
    The user can recover them within the grace window by restoring the
    most recent orphan as the new head (planned for v1.1).
    """
    project = await _owned_project(project_id, user, db)
    try:
        v = await restore_version(db, project, version_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return VersionOut(
        id                = str(v.id),
        parent_version_id = str(v.parent_version_id) if v.parent_version_id else None,
        prompt            = v.prompt,
        summary           = v.summary,
        created_at        = v.created_at,
        is_head           = True,
        orphaned_at       = None,
    )
