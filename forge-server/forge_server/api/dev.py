"""
forge_server/api/dev.py
========================
Dev server lifecycle API + SSE status stream.

Endpoints:
  POST /api/dev/ensure   — ensure container is running (create/wake)
  POST /api/dev/stop     — manually stop a container
  POST /api/dev/ping     — keep-alive heartbeat from forge-ui
  GET  /api/dev/status   — current container status (JSON)
  GET  /api/dev/logs     — last N log lines (JSON)
  GET  /api/dev/stream   — SSE stream of status events

The SSE stream is how forge-ui knows when the container moves from
"starting" → "running" and can activate the preview iframe.

forge-ui should:
  1. On session open → POST /api/dev/ensure
  2. Subscribe to GET /api/dev/stream?project_id=...
  3. Show loading overlay on preview panel
  4. On SSE event { status: "running" } → show iframe
  5. Send POST /api/dev/ping every 2 minutes while iframe is visible
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user, current_user_qs
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import DevContainer, Project, User
from forge_server.runner.container_manager import container_manager

log      = logging.getLogger("forge.dev")
settings = get_settings()
router   = APIRouter(prefix="/api/dev", tags=["dev"])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_project(project_id: str, user: User, db: AsyncSession) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _get_or_create_project(
    project_id:     str,
    user:           User,
    db:             AsyncSession,
    *,
    workspace_path: str | None = None,
    name:           str | None = None,
) -> Project:
    """
    Fetch the project, or auto-create it if it doesn't exist yet.
    This allows forge-ui to call /ensure with an opencode project ID
    on first use without needing a separate project-creation step.

    Ownership semantics:
      - project exists & owned by caller   → return it (optionally backfill workspace_path)
      - project exists & owned by someone  → 409, "not yours; clone via Use Template"
      - project does not exist             → auto-create under caller
    """
    # Look up by project_id alone so we can detect cross-user collisions.
    # Previously this filtered by user_id too, which silently fell through to
    # the auto-create branch and triggered a PK-uniqueness 500 — see the
    # showcased-project click scenario from May 2026.
    result  = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()

    if project is not None and str(project.user_id) == str(user.id):
        # Owned by caller — optionally backfill workspace_path and return.
        if workspace_path and not project.workspace_path:
            project.workspace_path = workspace_path
            await db.commit()
            await db.refresh(project)
        return project

    if project is not None:
        # Owned by someone else (e.g. a showcased project the caller is
        # browsing). Don't claim it; tell the FE to use the Use-Template flow,
        # which mints a new project_id and clones the workspace.
        raise HTTPException(
            status_code=409,
            detail={
                "error":      "project_owned_by_other_user",
                "message":    "This project belongs to another user — clone it via Use Template to work on a copy.",
                "project_id": project_id,
            },
        )

    # Auto-create the project under the caller.
    from pathlib import Path
    ws = workspace_path or str(
        Path(settings.forge_data_root)
        / "users" / user.id
        / "projects" / project_id
        / "workspace"
    )
    Path(ws).mkdir(parents=True, exist_ok=True)

    project = Project(
        id             = project_id,
        user_id        = user.id,
        name           = name or f"project-{project_id[:8]}",
        workspace_path = ws,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    log.info("Auto-created project %s for user %s", project_id, user.id)
    return project


async def _get_or_create_dc(project_id: str, db: AsyncSession) -> DevContainer:
    result = await db.execute(
        select(DevContainer).where(DevContainer.project_id == project_id)
    )
    dc = result.scalar_one_or_none()
    if not dc:
        dc = DevContainer(
            project_id    = project_id,
            status        = "stopped",
            internal_port = settings.container_dev_port,
            preview_url   = container_manager.preview_url(project_id),
        )
        db.add(dc)
        await db.flush()
    return dc


# ── Schemas ───────────────────────────────────────────────────────────────────

class EnsureIn(BaseModel):
    project_id:     str
    workspace_path: str | None = None   # provided on first call; stored in DB
    name:           str | None = None   # human-readable name (for auto-created projects)
    env_vars:       dict[str, str] | None = None


class StopIn(BaseModel):
    project_id: str


class PingIn(BaseModel):
    project_id: str


class StatusOut(BaseModel):
    project_id:     str
    status:         str
    container_name: str | None
    preview_url:    str | None
    last_ping_at:   datetime | None


class LogsOut(BaseModel):
    project_id: str
    logs:       str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/ensure", status_code=202)
async def ensure(
    body: EnsureIn,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Ensure the project's dev server container is running.
    Returns immediately with { status: "starting" | "running" }.
    forge-ui subscribes to /api/dev/stream for the "running" event.
    """
    project = await _get_or_create_project(
        body.project_id, user, db,
        workspace_path=body.workspace_path,
        name=body.name,
    )
    dc      = await _get_or_create_dc(body.project_id, db)

    # No more Forge-file reseeding. AGENTS.md / opencode.json / skills now live
    # globally in the opencode container and project state lives in the DB —
    # there's nothing in the workspace for `create-next-app` to clobber.

    # Already running — nothing to do
    if dc.status == "running":
        await db.commit()
        return {"status": "running", "preview_url": dc.preview_url}

    # Mark as starting in DB immediately so the UI can show a spinner
    dc.status     = "starting"
    dc.preview_url = container_manager.preview_url(body.project_id)
    await db.commit()

    # Kick off container ensure in the background so we can return fast
    asyncio.create_task(
        _start_container_bg(body.project_id, user.id, dc.id, body.env_vars)
    )

    return {
        "status":      "starting",
        "preview_url": dc.preview_url,
        "message":     "Container starting — subscribe to /api/dev/stream for status updates",
    }


async def _start_container_bg(
    project_id: str,
    user_id:    str,
    dc_id:      str,
    env_vars:   dict[str, str] | None,
) -> None:
    """
    Background task: create/start the container, wait for health check,
    then update DB status and broadcast SSE event.
    """
    try:
        result = await container_manager.ensure(project_id, user_id, env_vars)
        action = result["action"]
        log.info("Container ensure project_id=%s action=%s", project_id, action)

        # Update DB: installing (warm) or creating (cold)
        interim = "installing" if action == "warm_start" else "creating"
        await _update_dc_status(project_id, interim, result.get("container_name"))
        _sse_broadcast(project_id, interim)

        # Wait for dev server to be ready.
        # Cold starts require npm install inside Docker (downloads all packages),
        # which can take 3-5 minutes on first run. Warm starts with a cached
        # node_modules volume are fast (~5s) but we still use the full budget
        # so retries after a timeout don't give up prematurely.
        ready = await container_manager.health_check(project_id, timeout=300.0)

        final_status = "running" if ready else "crashed"
        await _update_dc_status(project_id, final_status, result.get("container_name"))
        _sse_broadcast(project_id, final_status)

    except Exception as exc:
        log.error("Container start failed project_id=%s: %s", project_id, exc, exc_info=True)
        await _update_dc_status(project_id, "crashed", None)
        _sse_broadcast(project_id, "crashed", error=str(exc))


async def _update_dc_status(
    project_id:     str,
    status:         str,
    container_name: str | None = None,
) -> None:
    from forge_server.db.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DevContainer).where(DevContainer.project_id == project_id)
        )
        dc = result.scalar_one_or_none()
        if dc:
            dc.status = status
            if container_name:
                dc.container_name = container_name
            if status == "running":
                dc.started_at  = datetime.utcnow()
                dc.last_ping_at = datetime.utcnow()
            dc.updated_at = datetime.utcnow()
            await db.commit()


@router.post("/stop", status_code=200)
async def stop(
    body: StopIn,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    await _get_project(body.project_id, user, db)
    dc = await _get_or_create_dc(body.project_id, db)

    await container_manager.stop(body.project_id)
    dc.status     = "sleeping"
    dc.updated_at = datetime.utcnow()
    await db.commit()

    _sse_broadcast(body.project_id, "sleeping")
    return {"status": "sleeping"}


@router.post("/ping", status_code=200)
async def ping(
    body: PingIn,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Keep-alive heartbeat. forge-ui sends this every 2 minutes
    while the preview iframe is visible. Prevents the sleep worker
    from stopping the container.
    """
    await _get_project(body.project_id, user, db)
    result = await db.execute(
        select(DevContainer).where(DevContainer.project_id == body.project_id)
    )
    dc = result.scalar_one_or_none()
    if dc:
        dc.last_ping_at = datetime.utcnow()
        await db.commit()
    return {"ok": True}


@router.get("/status", response_model=StatusOut)
async def status(
    project_id: str        = Query(...),
    user:       User       = Depends(current_user),
    db:         AsyncSession = Depends(get_db),
):
    await _get_project(project_id, user, db)
    result = await db.execute(
        select(DevContainer).where(DevContainer.project_id == project_id)
    )
    dc = result.scalar_one_or_none()

    # Cross-check with Docker in case container died outside our control
    docker_status = await container_manager.status(project_id)
    db_status     = dc.status if dc else "not_found"

    # If Docker says it's not running but DB says running → mark crashed
    if docker_status in ("sleeping", "not_found") and db_status == "running":
        if dc:
            dc.status = "crashed"
            await db.commit()
        db_status = "crashed"

    return StatusOut(
        project_id     = project_id,
        status         = db_status,
        container_name = dc.container_name if dc else None,
        preview_url    = dc.preview_url if dc else None,
        last_ping_at   = dc.last_ping_at if dc else None,
    )


@router.get("/logs", response_model=LogsOut)
async def logs(
    project_id: str        = Query(...),
    tail:       int        = Query(150, ge=1, le=1000),
    user:       User       = Depends(current_user),
    db:         AsyncSession = Depends(get_db),
):
    await _get_project(project_id, user, db)
    log_text = await container_manager.logs(project_id, tail=tail)
    return LogsOut(project_id=project_id, logs=log_text)


# ── SSE stream ────────────────────────────────────────────────────────────────

# In-memory subscriber registry: project_id → list of asyncio.Queue
_subscribers: dict[str, list[asyncio.Queue]] = {}


def _sse_broadcast(project_id: str, status: str, error: str | None = None) -> None:
    """Push a status event to all SSE subscribers for this project."""
    payload = {"status": status}
    if error:
        payload["error"] = error
    for q in _subscribers.get(project_id, []):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


@router.get("/stream")
async def stream(
    project_id: str          = Query(...),
    user:       User         = Depends(current_user_qs),  # accepts ?token= too (EventSource)
    db:         AsyncSession = Depends(get_db),
):
    """
    Server-Sent Events stream for container status.

    forge-ui subscribes here after calling /ensure.
    Events: { status: "starting" | "creating" | "installing" | "running" | "crashed" | "sleeping" }

    The stream keeps the connection open until the client disconnects.
    """
    await _get_project(project_id, user, db)

    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers.setdefault(project_id, []).append(queue)

    # Send current status immediately on connect
    result = await db.execute(
        select(DevContainer).where(DevContainer.project_id == project_id)
    )
    dc = result.scalar_one_or_none()
    current = dc.status if dc else "not_found"
    await queue.put({"status": current})

    async def event_generator():
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                    data    = json.dumps(payload)
                    yield f"data: {data}\n\n"
                    if payload.get("status") in ("running", "crashed"):
                        # Terminal states — client will act, stream can idle
                        pass
                except asyncio.TimeoutError:
                    # Keep-alive ping so the connection doesn't drop
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            subs = _subscribers.get(project_id, [])
            if queue in subs:
                subs.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",   # disable Nginx buffering
        },
    )
