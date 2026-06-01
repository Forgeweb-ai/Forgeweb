"""
forge_server/app.py
====================
Main FastAPI application.

Services:
  - Auth (JWT register/login)
  - Projects (CRUD + workspace management)
  - Dev server (Docker container lifecycle + SSE)
  - Supabase integration
  - Config (forge.json + per-project env vars)

Project previews are not proxied here. Each project container carries
Traefik labels (see container_manager._traefik_labels) so Traefik routes
http(s)://{project_id}.{PREVIEW_DOMAIN}/ → forge-proj-{id}:3000 directly.

On startup:
  - DB tables created (idempotent)
  - Sleep worker background task started
"""
from __future__ import annotations

import asyncio
import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from forge_server.config import get_settings
from forge_server.db.database import init_db
from forge_server.runner.sleep_manager import sleep_worker

from forge_server.api.auth             import router as auth_router
from forge_server.api.projects         import router as projects_router
from forge_server.api.dev              import router as dev_router
from forge_server.api.supabase_routes  import router as supabase_router
from forge_server.api.supabase_oauth   import router as supabase_oauth_router
from forge_server.api.config_routes    import router as config_router
from forge_server.api.settings_routes  import router as settings_router
from forge_server.api.provider_routes  import router as provider_router
from forge_server.api.db_routes        import router as db_routes_router
from forge_server.api.verify_routes    import router as verify_routes_router
from forge_server.api.runtime_errors_routes import router as runtime_errors_router

settings = get_settings()

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("forge.app")


# ── Lifespan ──────────────────────────────────────────────────────────────────

async def _reconcile_container_states() -> None:
    """
    On startup, reset all non-terminal DevContainer statuses to a safe state
    so the frontend always calls /ensure after a server restart.

    Resets:
      - 'starting' / 'creating' / 'installing'
        → The background task managing these was killed by Ctrl+C.
          Reset to 'stopped'.

      - 'running'
        → The server was restarted.  The container may still be alive in
          Docker, but we can't trust the DB without re-running the health
          check.  Reset to 'sleeping' — the frontend will call /ensure, the
          container will warm-start in ~5 s (node_modules cached), and the
          health check will confirm readiness before marking 'running' again.
    """
    from sqlalchemy import select, update
    from datetime import datetime
    from forge_server.db.database import AsyncSessionLocal
    from forge_server.db.models import DevContainer

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(DevContainer))
        containers = result.scalars().all()

    reset_map = {
        "starting":   "stopped",
        "creating":   "stopped",
        "installing": "stopped",
        "running":    "sleeping",
    }

    fixed = 0
    for dc in containers:
        new_status = reset_map.get(dc.status)
        if not new_status:
            continue
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(DevContainer)
                .where(DevContainer.project_id == dc.project_id)
                .values(status=new_status, updated_at=datetime.utcnow())
            )
            await db.commit()
        log.info(
            "reconcile: '%s' → '%s' for project %s",
            dc.status, new_status, dc.project_id,
        )
        fixed += 1

    log.info("reconcile: reset %d container record(s) on startup", fixed)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("forge-server starting up…")
    await init_db()
    log.info("Database tables ready")

    # Fix any stale container statuses left over from a previous crash / Ctrl+C.
    # Must run before the sleep worker or any requests arrive.
    await _reconcile_container_states()

    # No dev-user bootstrap any more — every user (including the developer
    # working locally) signs up via /api/auth/register and goes through
    # the real email-verify + onboarding flow. "dev mode" just means
    # "running locally"; it does not grant or impersonate any user.
    if settings.dev_mode:
        log.info("DEV_MODE is on — running with local dev settings.")

    task = asyncio.create_task(sleep_worker())
    log.info("Sleep worker started")

    # Background snapshot worker — periodically uploads dirty workspaces to
    # Supabase Storage. No-op if SUPABASE_SERVICE_ROLE_KEY isn't configured.
    from forge_server.storage.worker import snapshot_worker
    snap_task = asyncio.create_task(snapshot_worker())

    yield

    task.cancel()
    snap_task.cancel()
    for t in (task, snap_task):
        try:
            await t
        except asyncio.CancelledError:
            pass

    log.info("forge-server shut down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "forge-server",
    description = "Dev server lifecycle, preview proxy, and Supabase integration for Forge",
    version     = "1.0.0",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = settings.cors_origins,
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(dev_router)
app.include_router(supabase_router)
app.include_router(supabase_oauth_router)
app.include_router(config_router)
app.include_router(settings_router)
app.include_router(provider_router)
app.include_router(db_routes_router)
app.include_router(verify_routes_router)
app.include_router(runtime_errors_router)

# ── Preview routing ───────────────────────────────────────────────────────────
# Project previews are reached at http(s)://{project_id}.{PREVIEW_DOMAIN}/
# and routed directly by Traefik via per-container Docker labels (see
# container_manager._traefik_labels). forge-server does not proxy preview
# traffic — this keeps Next.js/Vite asset paths, fetch URLs, HMR WebSockets,
# and React hydration working with stock framework defaults.


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "forge-server"}
