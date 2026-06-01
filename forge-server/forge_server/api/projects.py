"""
forge_server/api/projects.py
=============================
Project CRUD — create, list, get, delete.
Each project maps to an OpenCode workspace directory.

On project create, _scaffold_workspace() just creates the empty workspace
directory. Forge writes nothing else inside it — the workspace is 100% the
user's app. Anything Forge needs (skills, agent instructions, project state)
lives elsewhere:

  - Project state                → projects table (GET /api/projects/{id}/config)
  - AGENTS.md                    → /root/.config/opencode/AGENTS.md (global)
  - Platform opencode.json       → forge-opencode-config/opencode.json (global)
  - Skills (supabase, schema,
    forge-platform, design-pool,
    ui-ux-pro-max, terminal-…)   → /forge-skills/ inside the opencode container
"""
from __future__ import annotations

import base64
import io
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import DevContainer, Project, User

settings = get_settings()
router   = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ProjectIn(BaseModel):
    name:        str
    description: str = ""


class ProjectOut(BaseModel):
    id:                   str
    name:                 str
    description:          str
    workspace_path:       str
    stack:                str | None = None
    opencode_session_id:  str | None = None
    created_at:           datetime
    updated_at:           datetime
    container_status:     str = "not_found"
    preview_url:          str | None = None
    showcased_at:         datetime | None = None
    showcase_name:        str | None = None
    showcase_description: str | None = None
    thumbnail_url:        str | None = None
    # Starred state — non-null timestamp means the user starred this project.
    starred_at:           datetime | None = None
    # Lineage — set if this project was cloned from a template/showcase.
    # Used by the "By me" view to filter to projects the user actually created.
    forked_from_project_id: str | None = None


class StarIn(BaseModel):
    starred: bool


class ThumbnailIn(BaseModel):
    image_data: str  # data:image/jpeg;base64,... or data:image/png;base64,...


class ShowcaseIn(BaseModel):
    showcase_name:        str | None = None
    showcase_description: str | None = None


class CloneIn(BaseModel):
    name:        str | None = None
    description: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _workspace_path(user_id: str, project_id: str) -> str:
    return str(
        Path(settings.forge_data_root)
        / "users" / user_id
        / "projects" / project_id
        / "workspace"
    )


def _thumbnail_dir() -> Path:
    p = Path(settings.forge_data_root) / "thumbnails"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _project_out(project: Project, dc: DevContainer | None) -> ProjectOut:
    # Build thumbnail_url as an API path (served by forge-server)
    thumb_url: str | None = None
    if project.thumbnail_url:
        thumb_url = f"/api/projects/{project.id}/thumbnail"
    return ProjectOut(
        id                   = project.id,
        name                 = project.name,
        description          = project.description or "",
        workspace_path       = project.workspace_path,
        stack                = project.stack,
        opencode_session_id  = project.opencode_session_id,
        created_at           = project.created_at,
        updated_at           = project.updated_at,
        container_status     = dc.status if dc else "not_found",
        preview_url          = dc.preview_url if dc else None,
        showcased_at         = project.showcased_at,
        showcase_name        = project.showcase_name,
        showcase_description = project.showcase_description,
        thumbnail_url        = thumb_url,
        starred_at           = project.starred_at,
        forked_from_project_id = project.forked_from_project_id,
    )


def _scaffold_workspace(workspace_path: str, project_id: str, name: str, description: str) -> None:
    """
    Initialise a project workspace.

    The workspace starts EMPTY of source code (the AI populates it).
    But it MUST be `git init`-ed so opencode recognises it as a project
    worktree — otherwise opencode's session falls back to projectID="global"
    with worktree="/", and ALL agent file writes get resolved against the
    host root and silently no-op. See `worktree = ... && !data.vcs ? "/" : ...`
    in opencode/packages/opencode/src/project/project.ts.

    Everything else Forge needs (config, AGENTS.md, skills) lives elsewhere —
    we never write Forge state into the workspace.
    """
    import subprocess

    ws = Path(workspace_path)
    ws.mkdir(parents=True, exist_ok=True)

    # `git init` the workspace so opencode sees a real VCS root and pins the
    # session worktree to THIS directory (not "/"). Idempotent — `git init` on
    # an already-initialised repo is a no-op.
    if not (ws / ".git").exists():
        try:
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main", str(ws)],
                check=True,
                capture_output=True,
                timeout=10,
            )
            # Minimal user config so commits work without prompting if the
            # agent ever runs them. Local to this repo only.
            subprocess.run(
                ["git", "-C", str(ws), "config", "user.email", "agent@forge.local"],
                check=False, capture_output=True, timeout=5,
            )
            subprocess.run(
                ["git", "-C", str(ws), "config", "user.name", "Forge Agent"],
                check=False, capture_output=True, timeout=5,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            # If git isn't installed or fails, fall through. opencode will
            # still see the directory; worst case it falls back to "global"
            # and writes will fail loudly enough that someone notices.
            import logging
            logging.getLogger("forge.projects").warning(
                "git init failed for workspace %s: %s — opencode may misroute writes",
                workspace_path, exc,
            )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[ProjectOut])
async def list_projects(
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project, DevContainer)
        .outerjoin(DevContainer, Project.id == DevContainer.project_id)
        .where(Project.user_id == user.id)
        .order_by(Project.updated_at.desc())
    )
    rows = result.all()
    return [_project_out(p, dc) for p, dc in rows]


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectIn,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    # The UI sometimes passes the user's whole first prompt as `name` (when the
    # project is created from a blank-prompt landing page). `name` is a
    # VARCHAR(255) — long prompts blow up the insert. Truncate defensively and
    # stash the full text in `description` so nothing is lost.
    NAME_MAX = 80
    raw_name = (body.name or "Untitled project").strip()
    if len(raw_name) > NAME_MAX:
        # Cut on the nearest word boundary for a cleaner title.
        truncated = raw_name[:NAME_MAX].rsplit(" ", 1)[0] or raw_name[:NAME_MAX]
        short_name  = truncated + "…"
        description = body.description or raw_name
    else:
        short_name  = raw_name
        description = body.description or ""

    project = Project(
        user_id        = user.id,
        name           = short_name,
        description    = description,   # description is unbounded TEXT
        workspace_path = "",   # set after we have the ID
    )
    db.add(project)
    await db.flush()   # get project.id

    ws = _workspace_path(user.id, project.id)
    project.workspace_path = ws

    # Make sure the workspace dir exists. We don't write anything else into it —
    # see _scaffold_workspace docstring.
    _scaffold_workspace(ws, project.id, body.name, body.description)

    await db.commit()
    await db.refresh(project)
    return _project_out(project, None)


@router.get("/showcase", response_model=list[ProjectOut])
async def list_showcase(
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Return all projects the current user has showcased (showcased_at IS NOT NULL)."""
    result = await db.execute(
        select(Project, DevContainer)
        .outerjoin(DevContainer, Project.id == DevContainer.project_id)
        .where(Project.user_id == user.id, Project.showcased_at != None)  # noqa: E711
        .order_by(Project.showcased_at.desc())
    )
    rows = result.all()
    return [_project_out(p, dc) for p, dc in rows]


@router.get("/gallery", response_model=list[ProjectOut])
async def list_gallery(
    db: AsyncSession = Depends(get_db),
):
    """
    Public template gallery — returns ALL showcased projects from ALL users.
    No auth required; used by the Resources page.
    """
    result = await db.execute(
        select(Project, DevContainer)
        .outerjoin(DevContainer, Project.id == DevContainer.project_id)
        .where(Project.showcased_at != None)  # noqa: E711
        .order_by(Project.showcased_at.desc())
    )
    rows = result.all()
    return [_project_out(p, dc) for p, dc in rows]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project, DevContainer)
        .outerjoin(DevContainer, Project.id == DevContainer.project_id)
        .where(Project.id == project_id, Project.user_id == user.id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_out(row[0], row[1])


@router.post("/{project_id}/thumbnail", status_code=204)
async def upload_thumbnail(
    project_id: str,
    body: ThumbnailIn,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Accept a base64-encoded screenshot from the frontend and store it.
    image_data format: "data:image/jpeg;base64,<base64>" or "data:image/png;base64,<base64>"
    """
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        # Parse data URL: data:<mime>;base64,<data>
        header, encoded = body.image_data.split(",", 1)
        ext = "jpg" if "jpeg" in header else "png"
        img_bytes = base64.b64decode(encoded)
        out_path = _thumbnail_dir() / f"{project_id}.{ext}"
        out_path.write_bytes(img_bytes)
        project.thumbnail_url = str(out_path)
        await db.commit()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image_data: {exc}") from exc


@router.get("/{project_id}/thumbnail")
async def get_thumbnail(
    project_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Serve the stored thumbnail PNG/JPEG for a project.
    Public for showcased projects so gallery cards load without auth.
    """
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if not project or not project.thumbnail_url:
        raise HTTPException(status_code=404, detail="No thumbnail")
    path = Path(project.thumbnail_url)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail file missing")
    media_type = "image/jpeg" if path.suffix == ".jpg" else "image/png"
    return FileResponse(str(path), media_type=media_type)


@router.post("/{project_id}/clone", response_model=ProjectOut, status_code=201)
async def clone_project(
    project_id: str,
    body:       CloneIn,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Clone a project into a new workspace for the current user.

    Copies the source workspace directory (excluding build artefacts and
    secrets) into a fresh project, updates forge.json with the new project_id,
    and re-seeds Forge-managed files (AGENTS.md, opencode.json, skills).

    Used by the "Use template" flow on the showcase / template gallery.
    """
    # ── Fetch the original ────────────────────────────────────────────────────
    # Allow cloning your own project OR any publicly showcased project
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            (Project.user_id == user.id) | (Project.showcased_at != None),  # noqa: E711
        )
    )
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Project not found")

    # Prefer the forge-showcase clone as source (lighter, no secrets)
    showcase_ws = Path(settings.forge_data_root) / "forge-showcase" / project_id / "workspace"

    # ── Create the new Project row ────────────────────────────────────────────
    new_name = (body.name or "").strip() or f"{original.showcase_name or original.name} (copy)"
    new_desc = (body.description or "").strip() or (original.showcase_description or original.description or "")

    new_project = Project(
        user_id                = user.id,
        name                   = new_name,
        description            = new_desc,
        workspace_path         = "",   # set after we have the ID
        forked_from_project_id = original.id,   # lineage tracking
    )
    db.add(new_project)
    # Increment the source's clone counter in the same transaction so the count
    # never gets out of sync with the lineage rows.
    original.clone_count = (original.clone_count or 0) + 1
    await db.flush()   # get new_project.id

    ws_dest = _workspace_path(user.id, new_project.id)
    new_project.workspace_path = ws_dest

    # ── Copy the workspace (skip build artefacts + secrets + legacy forge bits)
    # Prefer the pre-cleaned forge-showcase clone; fall back to live workspace.
    # We also skip any legacy forge.json / AGENTS.md / .opencode/ that may still
    # exist in older source workspaces — project state lives in the DB now and
    # the fork has its own new project_id, so copying those would just bake the
    # old project's identity into the clone.
    src = showcase_ws if showcase_ws.exists() else Path(original.workspace_path)
    if src.exists():
        shutil.copytree(
            src, ws_dest,
            ignore=shutil.ignore_patterns(
                "node_modules", ".git", "__pycache__", "*.pyc",
                "dist", "build", ".next", ".venv", "venv", ".env", ".env.*",
                "forge.json", "AGENTS.md", ".opencode",
            ),
        )
    else:
        # Source workspace missing — scaffold a fresh empty one
        _scaffold_workspace(ws_dest, new_project.id, new_name, new_desc)

    await db.commit()
    await db.refresh(new_project)
    return _project_out(new_project, None)


def _clone_to_showcase(project_id: str, workspace_path: str) -> None:
    """
    Background task: copy the project workspace into the shared forge-showcase
    directory so it can be used as a template by any user.

    Destination: {forge_data_root}/forge-showcase/{project_id}/workspace/

    Build artefacts (.git, node_modules, .venv, __pycache__, dist, .next,
    .opencode/sessions) are excluded to keep the clone lightweight.
    """
    import logging
    log = logging.getLogger(__name__)

    src = Path(workspace_path)
    if not src.exists():
        log.warning("showcase clone: source %s does not exist", src)
        return

    dst = Path(settings.forge_data_root) / "forge-showcase" / project_id / "workspace"
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)

    _EXCLUDE = {
        ".git", "node_modules", ".venv", "__pycache__",
        "dist", "build", ".next", ".nuxt", ".output",
        ".opencode",
    }

    def _ignore(directory: str, contents: list[str]) -> set[str]:
        return {c for c in contents if c in _EXCLUDE}

    try:
        shutil.copytree(str(src), str(dst), ignore=_ignore, dirs_exist_ok=True)
        log.info("showcase clone: %s → %s", src, dst)
    except Exception as exc:
        log.warning("showcase clone failed for %s: %s", project_id, exc)


async def _capture_screenshot_bg(project_id: str, preview_url: str) -> None:
    """
    Background task: take a headless-Chrome screenshot of the preview and store it.
    Requires `pip install playwright && playwright install chromium`.
    Silently skipped if playwright is not installed.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore[import]
    except ImportError:
        return  # playwright not installed — no thumbnail

    out_path = _thumbnail_dir() / f"{project_id}.jpg"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(preview_url, wait_until="networkidle", timeout=20_000)
            await page.screenshot(path=str(out_path), type="jpeg", quality=80,
                                  clip={"x": 0, "y": 0, "width": 1280, "height": 800})
            await browser.close()

        # Persist the path to the DB using a fresh session
        from forge_server.db.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(Project).where(Project.id == project_id))
            proj = res.scalar_one_or_none()
            if proj:
                proj.thumbnail_url = str(out_path)
                await db.commit()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Screenshot capture failed for %s: %s", project_id, exc)


@router.post("/{project_id}/showcase", response_model=ProjectOut)
async def showcase_project(
    project_id: str,
    body:       ShowcaseIn,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Mark a project as showcased (adds it to the home-page gallery).
    Accepts optional showcase_name and showcase_description to label the card.
    Triggers a background playwright screenshot automatically.
    """
    result = await db.execute(
        select(Project, DevContainer)
        .outerjoin(DevContainer, Project.id == DevContainer.project_id)
        .where(Project.id == project_id, Project.user_id == user.id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    project, dc = row
    project.showcased_at         = datetime.utcnow()
    project.showcase_name        = body.showcase_name or project.name
    project.showcase_description = body.showcase_description or project.description or ""
    await db.commit()
    await db.refresh(project)

    # Clone workspace → forge-showcase/{project_id}/ (background, best-effort)
    background.add_task(_clone_to_showcase, project_id, project.workspace_path)

    # Kick off a background screenshot capture (no-op if playwright not installed).
    from forge_server.runner.container_manager import _preview_url
    background.add_task(_capture_screenshot_bg, project_id, _preview_url(project_id))

    return _project_out(project, dc)


@router.delete("/{project_id}/showcase", status_code=204)
async def unshowcase_project(
    project_id: str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Remove a project from the showcase."""
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project.showcased_at = None
    await db.commit()

    # Remove forge-showcase clone
    showcase_dir = Path(settings.forge_data_root) / "forge-showcase" / project_id
    if showcase_dir.exists():
        shutil.rmtree(showcase_dir, ignore_errors=True)


@router.patch("/{project_id}/star", response_model=ProjectOut)
async def star_project(
    project_id: str,
    body:       StarIn,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Toggle whether the calling user has starred this project.

    Stored as a TIMESTAMPTZ — null = unstarred, non-null = when starred.
    Idempotent: starring an already-starred project leaves starred_at unchanged
    (we don't want sort order to jump on every click).
    """
    result = await db.execute(
        select(Project, DevContainer)
        .outerjoin(DevContainer, Project.id == DevContainer.project_id)
        .where(Project.id == project_id, Project.user_id == user.id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    project, dc = row

    if body.starred:
        if project.starred_at is None:
            project.starred_at = datetime.utcnow()
    else:
        project.starred_at = None

    await db.commit()
    await db.refresh(project)
    return _project_out(project, dc)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Stop + remove Docker container (best-effort — don't fail if container gone)
    try:
        from forge_server.runner.container_manager import container_manager
        await container_manager.remove(project_id)
    except Exception:
        pass

    # Delete workspace directory from disk
    workspace = Path(project.workspace_path)
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
        # Also clean up empty parent project dir
        parent = workspace.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()

    # Remove showcase clone if it exists
    showcase_dir = Path(settings.forge_data_root) / "forge-showcase" / project_id
    if showcase_dir.exists():
        shutil.rmtree(showcase_dir, ignore_errors=True)

    # Remove thumbnail file
    if project.thumbnail_url:
        try:
            Path(project.thumbnail_url).unlink(missing_ok=True)
        except Exception:
            pass

    await db.delete(project)
    await db.commit()


# ── Download (zip) ────────────────────────────────────────────────────────────

# Files/dirs we never want in a user-facing download
_ZIP_EXCLUDE_DIRS = {
    # build artefacts
    "node_modules", ".next", ".nuxt", "dist", "build", "out", ".turbo",
    # caches / virtualenvs
    ".cache", "__pycache__", ".venv", "venv",
    # version control
    ".git",
    # forge / opencode internals — never expose to the user
    "opencode", ".opencode", ".forge",
    # OS junk
    ".DS_Store",
}

# Individual filenames excluded at any depth
_ZIP_EXCLUDE_FILES = {"AGENTS.md", "forge.json", "opencode.json"}

def _should_exclude(rel: Path) -> bool:
    """Return True if any path segment or filename should be kept out of the zip."""
    if any(part in _ZIP_EXCLUDE_DIRS for part in rel.parts):
        return True
    if rel.name in _ZIP_EXCLUDE_FILES:
        return True
    return False


@router.get("/{project_id}/download")
async def download_project(
    project_id: str,
    user: User = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Stream the project workspace as a ZIP file.

    Excludes build artefacts (node_modules, .next, dist, __pycache__, .git,
    .venv, etc.) so the archive is small and immediately runnable after
    `npm install` / `pip install`.
    """
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    workspace = Path(project.workspace_path)
    if not workspace.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Build the zip in-memory (fast enough for typical web projects)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(workspace.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(workspace)
            if _should_exclude(rel):
                continue
            zf.write(file_path, arcname=rel)
    buf.seek(0)

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in project.name)
    filename = f"{safe_name or 'project'}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
