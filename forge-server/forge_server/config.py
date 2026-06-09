"""
forge_server/config.py
======================
Central config — reads from environment variables with sensible defaults.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    # Postgres only — defaults to the local Supabase Postgres that dev.sh
    # boots automatically. Override via DATABASE_URL env var for production
    # (hosted Supabase pooler URL).
    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:54322/postgres"

    # ── Redis (heartbeat store) ───────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── JWT auth ─────────────────────────────────────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7   # 7 days

    # ── Internal service auth ────────────────────────────────────────────────
    # Shared HMAC secret between forge-server's opencode_proxy and the opencode
    # fork's forge-user middleware. Used to sign X-Forge-User-Id per request so
    # opencode-side calls back into /api/internal/* can be trusted without
    # passing a user JWT through the proxy chain. MUST be overridden in prod —
    # see [[forge_storage_architecture]] for the secret-management plan.
    forge_internal_secret: str = "change-me-in-production-internal"

    # ── OpenCode ─────────────────────────────────────────────────────────────
    opencode_url: str = "http://opencode:7777"

    # ── Docker / containers ───────────────────────────────────────────────────
    docker_network: str = "forge-net"
    container_prefix: str = "forge-proj"
    # Base image for project containers — Node 20 + bun + Python 3
    container_image: str = "forge-runner:latest"
    # Internal port every project dev-server listens on
    container_dev_port: int = 3000

    # ── Preview / routing ─────────────────────────────────────────────────────
    # Local dev uses *.preview.lvh.me (resolves to 127.0.0.1 with no host setup).
    # Prod uses *.preview.forge.com via real wildcard DNS.
    preview_domain: str = "preview.lvh.me"
    # Scheme for preview URLs. http in local (no TLS cert for lvh.me),
    # https in prod (real cert from ACME).
    preview_scheme: str = "http"
    # Browser-facing forge-server base URL. The agent embeds AI-generated
    # images as <img src="<this>/forge-img/{slot}.png"> in the user's app —
    # because the preview iframe is served from *.preview.lvh.me (the
    # project container, NOT forge-server), a relative `/forge-img/...`
    # would 404 in-iframe. Must be reachable from the user's browser.
    # Dev default = the forge-server listen address; prod must override.
    forge_public_base_url: str = "http://127.0.0.1:8000"

    # ── Workspace paths ───────────────────────────────────────────────────────
    forge_data_root: str = "/forge-data"
    # Host-side path of forge_data_root, used ONLY when forge-server runs
    # inside Docker and asks the daemon to bind-mount the workspace into a
    # child container. The daemon resolves bind sources against the HOST
    # filesystem, not forge-server's container, so a raw `/forge-data` (an
    # in-container path) makes the mount silently fail on macOS Docker
    # Desktop and the child container stays stuck in `Created`. dev.sh and
    # any non-Docker run can leave this empty — the helper falls back to
    # forge_data_root, which IS the host path in those cases.
    forge_data_root_host: str = ""

    # ── Sleep worker ──────────────────────────────────────────────────────────
    # How often the sleep worker wakes (seconds)
    sleep_check_interval: int = 60
    # Containers idle longer than this are stopped (seconds)
    idle_ttl: int = 600    # 10 minutes

    # ── Project container resource limits ─────────────────────────────────────
    # Memory ceiling per project container. 1 GB is not enough — a fresh
    # Next.js pnpm install regularly peaks above that during the resolve pass
    # and gets OOM-killed mid-bootstrap, leaving the FE staring at "(no
    # output yet)". 2 GB is the smallest value that completes reliably across
    # the templates Forge ships. Operators on tight hosts can lower it via
    # FORGE_CONTAINER_MEM_LIMIT; the format is docker-py's mem_limit string
    # (e.g. "2g", "1536m").
    container_mem_limit: str = "2g"
    # CPU ceiling per project container in nano-CPUs (1e9 == 1 vCPU).
    container_nano_cpus: int = 1_000_000_000

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["*"]

    # ── Supabase Storage (for project snapshots) ──────────────────────────────
    # Local dev: dev.sh exports the keys from `supabase start` output.
    # Prod: set in environment, pointing at hosted Supabase project.
    supabase_url:              str = "http://127.0.0.1:54321"
    supabase_service_role_key: str = ""   # required for storage ops; injected from env
    snapshots_bucket:          str = "forge-snapshots"
    # How often the background worker scans for projects that need snapshotting.
    snapshot_check_interval:   int = 300   # 5 minutes
    # Minimum time since last snapshot before we consider a project for a new one
    snapshot_min_age:          int = 600   # 10 minutes

    # ── Supabase OAuth (BYOK for user apps) ───────────────────────────────────
    # Register an OAuth app at https://supabase.com/dashboard/account/apps
    # and paste the credentials here. Until set, the "Connect Supabase" flow
    # will return a clear error pointing the developer to add them.
    supabase_oauth_client_id:     str = ""
    supabase_oauth_client_secret: str = ""
    supabase_oauth_redirect_uri:  str = "http://localhost:8000/api/supabase/oauth/callback"

    # ── Forge runtime mode ────────────────────────────────────────────────────
    # Determines the DB strategy for generated user apps:
    #   "local-self-host" — (default, OSS) generated apps use the same Postgres
    #       Forge runs on, isolated per-project via schema (app_<project_id>) +
    #       a per-project role with grants only on that schema. One DB instance
    #       does both Forge metadata AND user-app data — no second Supabase to
    #       run. This is the OSS / hobbyist experience.
    #   "hosted" — (Forge SaaS only) BYO Supabase via OAuth, per user. Forge
    #       never holds user-app data in hosted mode — see LAUNCH_PLAN §2/§6c
    #       constraint to avoid becoming a multi-tenant data plane at scale.
    # The `/api/projects/{id}/db/provision` endpoint refuses to run in hosted
    # mode; the supabase.md skill branches on this flag.
    forge_mode: str = "local-self-host"

    # ── Dev mode ──────────────────────────────────────────────────────────────
    # DEV_MODE=true is just a "running locally" flag — useful for future
    # log-level / debug-route hooks. It does NOT bypass auth or auto-create
    # any user. Local dev uses the same real registration + login + onboarding
    # flow as production. Every user owns their own projects.
    dev_mode: bool = False

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # Ignore env vars that aren't declared above. .env is shared with
        # other consumers (Traefik, Docker, dev.sh), so forge-server must
        # not crash on keys it doesn't recognize. Default in
        # pydantic-settings v2 is "forbid", which broke startup.
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
