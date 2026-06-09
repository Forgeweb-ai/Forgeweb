"""
forge_server/api/preferences_routes.py
========================================
User preferences ("skills.md") API.

Endpoints:
  GET  /api/me/preferences  — read the current user's preferences markdown
  PUT  /api/me/preferences  — replace the preferences markdown

A single free-form markdown blob per user. Injected into every project
session's system prompt per-turn so the AI remembers user standing
preferences across all of their apps ("always use Tailwind", "snake_case DB
columns", etc.). Always loaded — NOT description-triggered — because user
intent is "this applies everywhere I work."

Storage path (single source of truth):
  - DB:   users.preferences_md (Text, nullable)
  - Disk: /forge-data/users/<user_id>/preferences.md
          (materialized on save so opencode can read it per-turn without a
          DB roundtrip; the read is on the hot path so disk + fs-cache beats
          DB-query-per-turn at scale.)

Per-project hookup is automatic via a symlink at
  /forge-data/users/<uid>/projects/<pid>/preferences.md
  → /forge-data/users/<uid>/preferences.md
The opencode-side system-prompt injector reads from the project-root
location (one level above workspace) — see opencode/src/forge/user-preferences.ts.
Symlink (not file copy) means one source-of-truth; edits to the user's
preferences immediately affect every existing project of theirs on next
session turn (instruction.system() is read per-turn — verified, see
opencode/src/session/instruction.ts).

Per §3 token-cost shape: flat. preferences_md is included in the system
prompt (prompt-cacheable, stable across turns of the same session). Empty
or NULL = zero added tokens.

Per §2 scale: at 100k users × ~500 tokens avg blob × N projects per user,
this is bounded and predictable. Symlinks (not file copies) mean the disk
footprint is one file per user, not N per user.

Cap: 100 KB to bound per-turn cost. ~500 tokens is the sane default
(~2KB); 100KB is a hard ceiling so a runaway paste doesn't blow up every
turn's BYOK bill.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from forge_server.api.auth import current_user
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import Project, User

log      = logging.getLogger("forge.preferences")
settings = get_settings()
router   = APIRouter(prefix="/api/me", tags=["user-preferences"])

# Cap the blob to bound per-turn token cost. 100 KB ≈ 25k tokens — well
# beyond what any human writes as "preferences"; serves as a backstop
# against a runaway paste, not a target.
MAX_PREFERENCES_BYTES = 100 * 1024


# ── Schemas ───────────────────────────────────────────────────────────────────

class PreferencesIn(BaseModel):
    # Empty string is a valid value — it clears the user's preferences.
    # `None` would be ambiguous with "field omitted"; require explicit text.
    content: str = Field(default="", max_length=MAX_PREFERENCES_BYTES)


class PreferencesOut(BaseModel):
    content: str            # "" if user has no preferences set
    bytes:   int            # len(content.encode()) — handy for FE token-count display
    updated: bool = False   # PUT response only — True if persisted, False if no-op


# ── Paths ─────────────────────────────────────────────────────────────────────

def _user_preferences_disk_path(user_id: str) -> Path:
    """Materialized location opencode reads. One per user, regardless of
    how many projects they have."""
    return Path(settings.forge_data_root) / "users" / user_id / "preferences.md"


def _project_preferences_symlink_path(user_id: str, project_id: str) -> Path:
    """The per-project symlink pointing at the user's preferences file.
    Lives at the project root (one level above /workspace) so opencode can
    discover it via a deterministic path derived from ctx.directory.
    Workspace itself stays untouched (per the project workspace policy in
    api/projects.py)."""
    return (
        Path(settings.forge_data_root)
        / "users" / user_id
        / "projects" / project_id
        / "preferences.md"
    )


# ── Sync helpers ──────────────────────────────────────────────────────────────

def _materialize_preferences_file(user_id: str, content: str) -> None:
    """Write the user-level preferences.md to disk. Single source of truth
    for opencode to read. Idempotent — empty content deletes the file so
    opencode's existsSafe() check yields false (== inject nothing)."""
    path = _user_preferences_disk_path(user_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if content:
            # Atomic write: write to a temp file in the same dir, then rename.
            # Avoids opencode reading a half-written file mid-edit.
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
        else:
            # Empty == cleared. Remove the file so the existsSafe() check
            # opencode-side returns false (= zero tokens injected).
            if path.exists() or path.is_symlink():
                path.unlink()
    except OSError as exc:
        # Disk write failures shouldn't fail the user's PUT silently — but
        # we also don't want them to wipe their DB state. Log loudly so we
        # spot drift; the DB is still the source of truth and a follow-up
        # save will retry materialization.
        log.error("failed to materialize preferences for user %s: %s", user_id, exc)


def _ensure_project_symlink(user_id: str, project_id: str) -> None:
    """Make sure the per-project symlink exists. Idempotent.

    Called from two places:
      1. PUT /api/me/preferences — repair any missing symlinks across the
         user's existing projects (one-time cost; cheap at <100 projects).
      2. Project create — link the new project on first creation so the
         user's standing prefs apply from the first turn.

    Symlink target is intentionally absolute so the link stays valid even
    if the project dir is moved (it currently never is, but defensive).
    """
    link  = _project_preferences_symlink_path(user_id, project_id)
    target = _user_preferences_disk_path(user_id)
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        # If a stale link/file exists at the link path, replace it. We can't
        # blindly unlink — there could legitimately be no link (first-time
        # create) — so check first.
        if link.is_symlink() or link.exists():
            if link.is_symlink() and Path(os.readlink(link)) == target:
                return  # already correct, no-op
            link.unlink()
        link.symlink_to(target)
    except OSError as exc:
        log.error(
            "failed to symlink preferences for user %s project %s: %s",
            user_id, project_id, exc,
        )


async def _sync_all_user_projects(user: User, db: AsyncSession) -> int:
    """Ensure every project the user owns has the preferences symlink.
    Returns the number of projects touched. Called on PUT so a brand-new
    preferences blob propagates to all existing projects immediately."""
    result = await db.execute(select(Project.id).where(Project.user_id == user.id))
    project_ids = [row[0] for row in result.all()]
    for pid in project_ids:
        _ensure_project_symlink(user.id, pid)
    return len(project_ids)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/preferences", response_model=PreferencesOut)
async def get_preferences(user: User = Depends(current_user)):
    """Return the user's preferences markdown. Empty string if unset."""
    content = user.preferences_md or ""
    return PreferencesOut(content=content, bytes=len(content.encode("utf-8")))


@router.put("/preferences", response_model=PreferencesOut)
async def put_preferences(
    body: PreferencesIn,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Replace the user's preferences markdown.

    Side effects on success:
      1. users.preferences_md updated in Postgres (source of truth).
      2. /forge-data/users/<uid>/preferences.md materialized to disk.
      3. Every existing project gets/keeps its symlink so the new content
         applies on their next AI turn (instructions are re-read per turn,
         no opencode cache invalidation needed).

    No-op short-circuit if content matches what's already stored — saves a
    DB write and a fs touch on save-without-edit clicks.
    """
    # Pydantic enforces max_length, but defense-in-depth: also reject if
    # the byte length blows past the cap (str length != byte length for
    # non-ASCII).
    if len(body.content.encode("utf-8")) > MAX_PREFERENCES_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"preferences exceed {MAX_PREFERENCES_BYTES} bytes",
        )

    current = user.preferences_md or ""
    if current == body.content:
        # No-op. Still ensure disk + symlinks are healthy in case prior
        # write failed; this is the user's chance to self-heal.
        _materialize_preferences_file(user.id, body.content)
        await _sync_all_user_projects(user, db)
        return PreferencesOut(
            content=body.content,
            bytes=len(body.content.encode("utf-8")),
            updated=False,
        )

    user.preferences_md = body.content or None  # store NULL when blob is empty
    await db.commit()
    await db.refresh(user)

    _materialize_preferences_file(user.id, body.content)
    touched = await _sync_all_user_projects(user, db)
    log.info(
        "User %s updated preferences (%d bytes, %d projects synced)",
        user.id, len(body.content.encode("utf-8")), touched,
    )

    return PreferencesOut(
        content=body.content,
        bytes=len(body.content.encode("utf-8")),
        updated=True,
    )
