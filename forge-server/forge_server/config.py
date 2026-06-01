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

    # ── Workspace paths ───────────────────────────────────────────────────────
    forge_data_root: str = "/forge-data"

    # ── Sleep worker ──────────────────────────────────────────────────────────
    # How often the sleep worker wakes (seconds)
    sleep_check_interval: int = 60
    # Containers idle longer than this are stopped (seconds)
    idle_ttl: int = 600    # 10 minutes

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
        # other consumers (opencode reads ANTHROPIC_API_KEY from the same
        # file), so forge-server must not crash on keys it doesn't recognize.
        # Default in pydantic-settings v2 is "forbid", which broke startup.
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
