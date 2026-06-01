"""
forge_server/api/verify_routes.py
==================================
Post-completion verification endpoint for the verify subagent.

Endpoint:
  POST /api/projects/{project_id}/verify

What it does, server-side, in one round-trip:
  1. ensure() the container — starts it if not running, no-op if already up
  2. health_check() — wait until dev server binds to :3000
  3. HTTP-probe the preview ("/") and each project-config endpoint marked
     accessible, from inside the forge-net Docker network
  4. Tail the last ~200 lines of container logs and parse for known error
     signatures (compile errors, missing modules, port collisions, 5xx
     server errors, unhandled promise rejections)
  5. Return a structured report the verify subagent can act on without
     needing docker access or shell scripts

Design notes:
  - The verify subagent never talks to docker directly. ALL container
    introspection happens here. That's the whole point — one source of truth
    for "what is this app doing right now".
  - This endpoint is idempotent and safe to call any time. The continuous
    log watcher (see runner/log_watcher.py, next milestone) will also fire
    verify based on errors detected outside this code path.
  - We deliberately do NOT include raw log lines >500 chars in the response.
    The verify subagent is instructed to translate findings into plain English
    before surfacing to the user; we keep the raw payload bounded.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import Project, User
from forge_server.runner.container_manager import (
    container_manager,
    _container_name,
    _host_port,
)
from forge_server.runner.log_parser import parse_tail
from forge_server.runner.log_watcher import recent_errors

log      = logging.getLogger("forge.verify")
settings = get_settings()
router   = APIRouter(prefix="/api/projects", tags=["verify"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class LogError(BaseModel):
    signature: str       # e.g. "missing_module", "ts_compile_error"
    detail:    str       # captured group (module name, error msg, port)
    line:      str       # original log line, truncated to 500 chars


class EndpointProbe(BaseModel):
    path:         str    # e.g. "/", "/api/books"
    status:       int    # HTTP status, 0 if connection refused / timeout
    body_snippet: str    # first 300 chars of response body, for 4xx/5xx
    error:        str | None = None


class VerifyReport(BaseModel):
    container_status: str                # "running" | "starting" | "crashed" | "not_found"
    preview_url:      str
    health_ok:        bool               # did dev server bind to :3000
    endpoint_probes:  list[EndpointProbe]
    log_errors:       list[LogError]
    fatal:            bool               # something verify cannot fix
    summary:          str                # one-line plain English for the agent


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_owned_project(
    project_id: str, user: User, db: AsyncSession
) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    p = result.scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Project not found")
    return p


def _container_base_url(project_id: str) -> str:
    """
    Where this forge-server process can reach the project container.
    Mirrors the logic in container_manager.health_check.
    """
    name = _container_name(project_id)
    in_docker = os.path.exists("/.dockerenv")
    if in_docker:
        return f"http://{name}:{settings.container_dev_port}"
    return f"http://127.0.0.1:{_host_port(project_id)}"


async def _probe_endpoint(
    client: httpx.AsyncClient, base: str, path: str
) -> EndpointProbe:
    """Single HTTP probe. Never raises — packages all failure modes."""
    url = base.rstrip("/") + "/" + path.lstrip("/")
    try:
        r = await client.get(url, timeout=5.0)
        body = ""
        if r.status_code >= 400:
            body = (r.text or "")[:300]
        return EndpointProbe(path=path, status=r.status_code, body_snippet=body)
    except httpx.TimeoutException:
        return EndpointProbe(path=path, status=0, body_snippet="", error="timeout")
    except httpx.ConnectError as e:
        return EndpointProbe(path=path, status=0, body_snippet="", error=f"connect: {e}")
    except Exception as e:
        return EndpointProbe(path=path, status=0, body_snippet="", error=str(e))


def _parse_log_errors(logs: str) -> list[LogError]:
    """Adapter: shared parser returns dataclasses; we wrap them in the API model."""
    return [
        LogError(signature=e.signature, detail=e.detail, line=e.line)
        for e in parse_tail(logs)
    ]


def _summarize(report: dict[str, Any]) -> str:
    """One-line plain English the agent can lift into a user-facing message."""
    if not report["health_ok"]:
        return "The dev server isn't responding yet."
    bad_probes = [p for p in report["endpoint_probes"] if p["status"] >= 500 or p["status"] == 0]
    if bad_probes:
        return f"{len(bad_probes)} endpoint(s) returned errors."
    errs = report["log_errors"]
    if errs:
        return f"{errs[0]['signature']}: {errs[0]['detail'] or 'see logs'}".strip(": ")
    return "Everything looks healthy."


# ── Route ────────────────────────────────────────────────────────────────────

@router.post("/{project_id}/verify", response_model=VerifyReport)
async def verify(
    project_id: str,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Ensure → probe → parse logs → report. Single round-trip for the verify
    subagent. Idempotent; always reflects current container state.

    Cold-start budget: 120s for health_check. If the container needs longer
    (very first npm install on a brand-new project), the verify subagent
    should call this endpoint again on its next loop — we don't tie up the
    HTTP connection for 5 minutes.
    """
    project = await _get_owned_project(project_id, user, db)
    name    = _container_name(project_id)

    # 1. ensure the container is up. ensure() is idempotent.
    try:
        await container_manager.ensure(project_id, user.id, extra_env=None)
    except Exception as e:
        log.error("verify: ensure failed project_id=%s: %s", project_id, e, exc_info=True)
        return VerifyReport(
            container_status="crashed",
            preview_url=container_manager.preview_url(project_id),
            health_ok=False,
            endpoint_probes=[],
            log_errors=[],
            fatal=True,
            summary=f"Container failed to start: {e}",
        )

    # 2. wait for dev server to bind. 120s is enough for warm starts; on a
    #    true cold start (first npm install) the agent will re-call us.
    health_ok = await container_manager.health_check(project_id, timeout=120.0)

    # 3. probe known endpoints. v1 just hits "/". The follow-up will read
    #    project.config.endpoints from the DB and probe each one.
    base = _container_base_url(project_id)
    probes: list[EndpointProbe] = []
    if health_ok:
        async with httpx.AsyncClient() as client:
            paths = ["/"]
            # TODO: read project config for additional endpoint paths
            results = await asyncio.gather(
                *(_probe_endpoint(client, base, p) for p in paths)
            )
            probes = list(results)

    # 4. Combine errors from two sources:
    #    a) one-shot parse of the last 200 log lines (catches errors that
    #       happened before this endpoint was called, e.g. during cold start)
    #    b) the continuous watcher's recent-errors ring (covers errors that
    #       fired since the last verify run but before any new logs)
    #    Dedupe by (signature, detail) — newer wins.
    raw_logs = await container_manager.logs(project_id, tail=200)
    tail_errors = _parse_log_errors(raw_logs)
    watcher_errors = [
        LogError(signature=e["signature"], detail=e["detail"], line=e["line"])
        for e in recent_errors(project_id)
    ]
    by_key: dict[tuple[str, str], LogError] = {}
    for e in tail_errors + watcher_errors:
        by_key[(e.signature, e.detail)] = e
    log_errors = list(by_key.values())

    # 5. classify + summarise
    container_status = await container_manager.status(project_id)
    # Fatal = container won't start AND no log errors we can act on.
    # The verify subagent treats fatal as "give up cleanly".
    fatal = (container_status not in ("running", "starting")) and not log_errors

    payload = {
        "container_status": container_status,
        "preview_url":      container_manager.preview_url(project_id),
        "health_ok":        health_ok,
        "endpoint_probes":  [p.model_dump() for p in probes],
        "log_errors":       [e.model_dump() for e in log_errors],
        "fatal":            fatal,
    }
    payload["summary"] = _summarize(payload)

    return VerifyReport(**payload)
