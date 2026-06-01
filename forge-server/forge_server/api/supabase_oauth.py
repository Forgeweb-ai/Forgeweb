"""
forge_server/api/supabase_oauth.py
====================================
Per-user Supabase OAuth — the BYOK flow.

Lifecycle:
  1. FE → GET  /api/supabase/oauth/start           → { authorize_url, state }
  2. User clicks authorize_url, Supabase asks them to allow Forge.
  3. Supabase → GET /api/supabase/oauth/callback?code=...&state=...
     We exchange the code for an access_token, store it encrypted, redirect
     back to the FE with ?supabase_connected=1.
  4. FE → GET  /api/supabase/oauth/status          → { connected, email }
  5. FE → POST /api/supabase/oauth/disconnect      → removes the row.

We use the access_token to call Supabase's Management API to provision
projects on behalf of the user. The user remains the data owner — Forge
never holds their service_role_key, only the scoped delegation token.

Setup (one-time, manual):
  - Go to https://supabase.com/dashboard/account/apps
  - Create a new OAuth app:
    * Redirect URL: http://localhost:8000/api/supabase/oauth/callback
                    (dev) — change for prod
    * Scopes: at minimum, all:*
  - Copy the client_id + client_secret into forge-server/.env:
      SUPABASE_OAUTH_CLIENT_ID=...
      SUPABASE_OAUTH_CLIENT_SECRET=...
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user
from forge_server.api.supabase_routes import _encrypt, _decrypt
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import User, UserSupabaseOAuth

log      = logging.getLogger("forge.supabase.oauth")
settings = get_settings()
router   = APIRouter(prefix="/api/supabase/oauth", tags=["supabase-oauth"])

# Supabase OAuth + Management endpoints.
SUPABASE_AUTHORIZE_URL = "https://api.supabase.com/v1/oauth/authorize"
SUPABASE_TOKEN_URL     = "https://api.supabase.com/v1/oauth/token"
SUPABASE_API_BASE      = "https://api.supabase.com/v1"

# In-memory state store for CSRF protection. For multi-instance deploys this
# should move to Redis (forge-server already has Redis for the sleep store).
# State entries are short-lived (5 min) so memory churn is bounded.
_state_store: dict[str, tuple[str, datetime]] = {}
_STATE_TTL   = timedelta(minutes=5)


def _check_oauth_config() -> None:
    if not settings.supabase_oauth_client_id or not settings.supabase_oauth_client_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "Supabase OAuth is not configured. Set SUPABASE_OAUTH_CLIENT_ID "
                "and SUPABASE_OAUTH_CLIENT_SECRET in forge-server/.env. See "
                "supabase_oauth.py module docstring for setup steps."
            ),
        )


def _gc_state() -> None:
    now = datetime.now(timezone.utc)
    expired = [k for k, (_uid, exp) in _state_store.items() if exp < now]
    for k in expired:
        _state_store.pop(k, None)


# ── Models ────────────────────────────────────────────────────────────────────

class StartOut(BaseModel):
    authorize_url: str
    state:         str


class StatusOut(BaseModel):
    connected:           bool
    supabase_user_email: Optional[str] = None


class ProvisionIn(BaseModel):
    name:     str
    region:   str   = "us-east-1"
    org_id:   str   # Supabase org the project lives under
    db_pass:  str   # Postgres root password the user wants


class ProvisionOut(BaseModel):
    project_ref: str
    anon_key:    str
    url:         str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/start", response_model=StartOut)
async def start(user: User = Depends(current_user)):
    """Returns the URL the FE should redirect the browser to."""
    _check_oauth_config()
    _gc_state()
    state = secrets.token_urlsafe(32)
    _state_store[state] = (str(user.id), datetime.now(timezone.utc) + _STATE_TTL)

    params = {
        "client_id":     settings.supabase_oauth_client_id,
        "redirect_uri":  settings.supabase_oauth_redirect_uri,
        "response_type": "code",
        "state":         state,
    }
    return StartOut(
        authorize_url=f"{SUPABASE_AUTHORIZE_URL}?{urlencode(params)}",
        state=state,
    )


@router.get("/callback")
async def callback(
    code:  str = Query(...),
    state: str = Query(...),
    db:    AsyncSession = Depends(get_db),
):
    """Supabase redirects the browser here. We exchange `code` for tokens,
    store them encrypted under the user we associated with `state`, then
    bounce the browser back to the FE."""
    _check_oauth_config()
    entry = _state_store.pop(state, None)
    if entry is None or entry[1] < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="OAuth state expired or unknown")
    user_id, _exp = entry

    async with httpx.AsyncClient(timeout=30.0) as client:
        token_resp = await client.post(
            SUPABASE_TOKEN_URL,
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  settings.supabase_oauth_redirect_uri,
                "client_id":     settings.supabase_oauth_client_id,
                "client_secret": settings.supabase_oauth_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            log.error("Supabase token exchange failed: %s %s",
                      token_resp.status_code, token_resp.text)
            raise HTTPException(status_code=502, detail="Token exchange failed")
        tokens = token_resp.json()

        # Fetch the user's email from Supabase so we can show "Connected as <email>"
        prof_resp = await client.get(
            f"{SUPABASE_API_BASE}/profile",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        sb_email = (prof_resp.json() or {}).get("primary_email") if prof_resp.status_code == 200 else None

    expires_at = None
    if "expires_in" in tokens:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens["expires_in"]))

    # Upsert by user_id
    result = await db.execute(
        select(UserSupabaseOAuth).where(UserSupabaseOAuth.user_id == user_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = UserSupabaseOAuth(user_id=user_id)
        db.add(row)
    row.access_token_enc    = _encrypt(tokens["access_token"])
    row.refresh_token_enc   = _encrypt(tokens["refresh_token"]) if tokens.get("refresh_token") else None
    row.expires_at          = expires_at
    row.supabase_user_email = sb_email
    await db.commit()

    log.info("User %s connected Supabase (email=%s)", user_id, sb_email)
    return RedirectResponse("/home?supabase_connected=1", status_code=302)


@router.get("/status", response_model=StatusOut)
async def status(
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserSupabaseOAuth).where(UserSupabaseOAuth.user_id == user.id)
    )
    row = result.scalar_one_or_none()
    return StatusOut(
        connected=row is not None,
        supabase_user_email=row.supabase_user_email if row else None,
    )


@router.post("/disconnect")
async def disconnect(
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserSupabaseOAuth).where(UserSupabaseOAuth.user_id == user.id)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
    return {"ok": True}


# ── Management API helpers (called by project provisioning) ──────────────────

async def _get_user_access_token(db: AsyncSession, user_id: str) -> str:
    result = await db.execute(
        select(UserSupabaseOAuth).where(UserSupabaseOAuth.user_id == user_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="Supabase not connected. Hit /api/supabase/oauth/start first.",
        )
    # Refresh-token handling deferred — Supabase access tokens are long-lived
    # (~1 day) and the user can reconnect if it expires.
    return _decrypt(row.access_token_enc)


@router.post("/provision", response_model=ProvisionOut)
async def provision_project(
    body: ProvisionIn,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Create a new Supabase project in the user's org and return its
    anon_key + URL. Forge stores no service-role key from this call."""
    access_token = await _get_user_access_token(db, str(user.id))
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{SUPABASE_API_BASE}/projects",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "name":             body.name,
                "organization_id":  body.org_id,
                "region":           body.region,
                "db_pass":          body.db_pass,
                "plan":             "free",
            },
        )
        if r.status_code not in (200, 201):
            log.error("Supabase project create failed: %s %s", r.status_code, r.text)
            raise HTTPException(status_code=502, detail=f"Supabase create failed: {r.text}")
        data = r.json()
        # Fetch the project's API keys
        ref = data["id"]
        keys = await client.get(
            f"{SUPABASE_API_BASE}/projects/{ref}/api-keys",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        keys.raise_for_status()
        kk = {k["name"]: k["api_key"] for k in keys.json()}
    return ProvisionOut(
        project_ref=ref,
        anon_key=kk.get("anon", ""),
        url=f"https://{ref}.supabase.co",
    )
