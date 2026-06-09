"""
forge_server/api/internal_routes.py
====================================
Internal service-to-service routes.

These endpoints are called by the opencode fork (NOT by the browser) to
resolve per-user runtime values that opencode can't read directly from the
DB. The shared opencode process serves many users concurrently — see
[[forge_two_product_model]] — so user identity must arrive on every request.

Auth model:
  - forge-server's opencode_proxy adds two headers on every browser→opencode
    request (Task #4): `X-Forge-User-Id` and `X-Forge-Internal-Token`.
  - The token = HMAC-SHA256(forge_internal_secret, f"{user_id}:{minute_bucket}").
  - The opencode-side middleware (`forge-user.ts`) verifies that token and
    exposes the user-id to opencode handlers; when opencode calls back into
    these endpoints, it FORWARDS the same two headers verbatim.
  - `verify_internal_token` here re-verifies the HMAC against the current
    minute and the previous minute (clock-skew tolerance).

Cost shape (per the BYOK + scale rules in CLAUDE.md):
  - Endpoint is hit at most 2× per user turn (design-analyst + design-critic
    share a cache key inside opencode), so flat under 100k containers.
  - In-process 60s TTL cache on the DB read keeps Postgres traffic O(users
    actively building) regardless of dispatch frequency.
"""
from __future__ import annotations

import hmac
import json
import logging
import time
from hashlib import sha256
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.constants import AGENT_SETTING_KEYS, SETTINGS_DEFAULTS
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import Project, UserSettings
from forge_server.storage.versions import create_version

log      = logging.getLogger("forge.internal")
settings = get_settings()
router   = APIRouter(prefix="/api/internal", tags=["internal"])


# ── HMAC helpers ──────────────────────────────────────────────────────────────

# 60-second window. Keeps replay surface small without making clock skew a
# constant failure mode. Increase only if the proxy ↔ opencode clocks
# genuinely drift more than a minute apart (they shouldn't — same host or
# same docker network).
_TOKEN_WINDOW_SECONDS = 60


def _sign(user_id: str, minute_bucket: int) -> str:
    """HMAC-SHA256(secret, f'{user_id}:{minute_bucket}'), hex-encoded.

    Lowercase hex to make string-comparison failures impossible. Constant-time
    comparison is the caller's responsibility (`hmac.compare_digest`).
    """
    msg = f"{user_id}:{minute_bucket}".encode("utf-8")
    key = settings.forge_internal_secret.encode("utf-8")
    return hmac.new(key, msg, sha256).hexdigest()


def _sign_project(project_id: str, minute_bucket: int) -> str:
    """HMAC-SHA256(secret, f'project:{project_id}:{minute_bucket}'), hex-encoded.

    Project-scoped variant for plugin → forge-server internal calls that
    aren't user-bound at the call site. Domain-separated from `_sign` by
    the literal "project:" prefix so a token signed for user U cannot be
    replayed against a project endpoint that happens to share the same
    UUID string (defensive — Forge user IDs and project IDs come from
    different UUID namespaces, but it's free to enforce).
    """
    msg = f"project:{project_id}:{minute_bucket}".encode("utf-8")
    key = settings.forge_internal_secret.encode("utf-8")
    return hmac.new(key, msg, sha256).hexdigest()


def verify_internal_project_token(
    project_id: str,
    x_forge_internal_token: Optional[str] = Header(None, alias="X-Forge-Internal-Token"),
) -> str:
    """FastAPI dependency: HMAC-verify a project-scoped internal token.

    Same clock-skew tolerance as verify_internal_token (current + previous
    minute). project_id comes from the URL path; caller binds it via
    Depends in the route signature.
    """
    if not x_forge_internal_token:
        raise HTTPException(status_code=401, detail="missing internal token")

    now_bucket = int(time.time()) // _TOKEN_WINDOW_SECONDS
    candidates = (_sign_project(project_id, now_bucket),
                  _sign_project(project_id, now_bucket - 1))

    if not any(hmac.compare_digest(c, x_forge_internal_token) for c in candidates):
        log.warning("Rejected internal project call: bad HMAC for project_id=%s", project_id)
        raise HTTPException(status_code=401, detail="invalid internal token")
    return project_id


def verify_internal_token(
    x_forge_user_id: Optional[str]        = Header(None, alias="X-Forge-User-Id"),
    x_forge_internal_token: Optional[str] = Header(None, alias="X-Forge-Internal-Token"),
) -> str:
    """FastAPI dependency. Returns the verified user_id, or 401.

    Verifies the HMAC against this minute's bucket AND the previous minute's
    bucket, so a request signed 30s before a minute roll-over still validates.
    """
    if not x_forge_user_id or not x_forge_internal_token:
        raise HTTPException(status_code=401, detail="missing internal auth headers")

    now_bucket = int(time.time()) // _TOKEN_WINDOW_SECONDS
    candidates = (_sign(x_forge_user_id, now_bucket),
                  _sign(x_forge_user_id, now_bucket - 1))

    if not any(hmac.compare_digest(c, x_forge_internal_token) for c in candidates):
        # Don't log the token value — it's a credential.
        log.warning("Rejected internal call: bad HMAC for user_id=%s", x_forge_user_id)
        raise HTTPException(status_code=401, detail="invalid internal token")

    return x_forge_user_id


# ── 60s in-process cache ──────────────────────────────────────────────────────

# Key: (user_id, setting_key). Value: (model_id_or_default, expires_at_unix).
# Bounded only by active-user count — not unbounded user growth — because we
# only insert on lookup and entries expire. At 100k MAU with normal dispatch
# patterns this stays under a few thousand live entries; can move to LRU
# if profiling ever shows it matters.
_cache: dict[tuple[str, str], tuple[str, float]] = {}
_CACHE_TTL_SECONDS = 60.0


def _cache_get(user_id: str, setting_key: str) -> Optional[str]:
    hit = _cache.get((user_id, setting_key))
    if not hit: return None
    value, expires_at = hit
    if expires_at < time.monotonic():
        _cache.pop((user_id, setting_key), None)
        return None
    return value


def _cache_put(user_id: str, setting_key: str, value: str) -> None:
    _cache[(user_id, setting_key)] = (value, time.monotonic() + _CACHE_TTL_SECONDS)


def invalidate_user_setting_cache(user_id: str, setting_key: str | None = None) -> None:
    """Drop cached entries for `user_id`. Public helper so mutation routes
    (settings PATCH, provider-key POST/DELETE) can tell opencode to re-read
    fresh data on its very next request instead of waiting up to
    _CACHE_TTL_SECONDS for the entry to expire.

    Without this, the typical user flow — save in UI → expect to see it
    immediately — fails for up to a minute because opencode keeps serving
    the stale resolver response. We bias the cache toward "stale on the
    way out, fresh on mutation"; the read side stays a fast in-process
    dict lookup, the write side pays one O(per-user-cache-entries) sweep
    (worst case 2 entries today: agent-model + custom-providers).

    `setting_key=None` invalidates every entry for the user — used by
    provider-key mutations that affect the `custom_providers.keyed` field
    inside the cached payload. Targeted invalidation is fine when only
    one bucket is known to be stale.
    """
    if setting_key is not None:
        _cache.pop((user_id, setting_key), None)
        return
    for k in list(_cache.keys()):
        if k[0] == user_id:
            _cache.pop(k, None)


# ── Schema ────────────────────────────────────────────────────────────────────

class AgentModelOut(BaseModel):
    # Resolved model id in `providerID/modelID` shape, OR null if the agent
    # name isn't user-configurable (caller should keep its existing default).
    model: Optional[str]


class CustomProvidersOut(BaseModel):
    """Per-user custom provider definitions, opencode-shaped.

    Keyed by providerID. Each value is the opencode provider config the user
    saved via the FE dialog — name/npm/options/models/headers. API keys are
    NOT in this payload; opencode resolves them per-request via the existing
    X-Forge-Auth path that decrypts from user_provider_keys.key_enc. This
    keeps secret material out of the cacheable resolver response.

    `keyed` lists providerIDs (from `providers` OR platform-owned) for which
    the user has a stored API key. opencode's `list` handler uses this to
    flag providers as "connected" without having to do its own per-provider
    key lookup. Plaintext key values are NEVER in this field — only IDs.
    """
    providers: dict[str, dict]
    keyed:     list[str]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/agent-model", response_model=AgentModelOut)
async def get_agent_model(
    agent:   str           = Query(..., description="Opencode agent name, e.g. 'design-analyst'"),
    user_id: str           = Depends(verify_internal_token),
    db:      AsyncSession  = Depends(get_db),
):
    """Resolve the calling user's configured model for `agent`.

    Returns `{model: null}` for agents that aren't mapped — opencode treats
    that as "no override, use static config", which is the safe fallback if
    a new agent appears in opencode before AGENT_SETTING_KEYS is updated.
    """
    setting_key = AGENT_SETTING_KEYS.get(agent)
    if not setting_key:
        return AgentModelOut(model=None)

    cached = _cache_get(user_id, setting_key)
    if cached is not None:
        return AgentModelOut(model=cached)

    result = await db.execute(
        select(UserSettings.settings_json).where(UserSettings.user_id == user_id)
    )
    row   = result.scalar_one_or_none()
    data  = json.loads(row or "{}")
    value = data.get(setting_key) or SETTINGS_DEFAULTS[setting_key]

    _cache_put(user_id, setting_key, value)
    return AgentModelOut(model=value)


@router.get("/custom-providers", response_model=CustomProvidersOut)
async def get_custom_providers(
    user_id: str           = Depends(verify_internal_token),
    db:      AsyncSession  = Depends(get_db),
):
    """Resolve the calling user's custom provider definitions.

    Called by the opencode fork at session-init time (mirrors the
    /agent-model resolver). Output is opencode-shaped — opencode merges it
    into its in-memory provider map for THIS user's session only. Keys are
    NEVER in this response; opencode pulls them per-request via the
    X-Forge-Auth path that decrypts from user_provider_keys.key_enc.

    Same 60s in-process cache as agent-model so the resolver can fire on
    every session start without flooding Postgres. Cache key is
    (user_id, "custom_providers"). Invalidation: the FE doesn't need to
    flush the cache after a save — the user just opens a new session.
    A future improvement could expose a /api/internal/cache/invalidate
    when we add live config reload.
    """
    cached = _cache_get(user_id, "custom_providers")
    if cached is not None:
        # Cache stores a JSON-serialised wrapper {providers, keyed} to keep
        # the cache layer generic (string-only).
        payload = json.loads(cached)
        return CustomProvidersOut(
            providers=payload.get("providers") or {},
            keyed=payload.get("keyed") or [],
        )

    result = await db.execute(
        select(UserSettings.settings_json).where(UserSettings.user_id == user_id)
    )
    row  = result.scalar_one_or_none()
    data = json.loads(row or "{}")
    providers = data.get("custom_providers") or {}
    if not isinstance(providers, dict):
        # Defensive: a malformed entry shouldn't break the resolver.
        providers = {}

    # Which providerIDs (custom OR platform) does the user have a key for?
    # opencode treats this as "connected" — without this, the user-custom
    # provider shows up in the dropdown but greyed out because the
    # `connected` filter in Forge mode requires p.key or p.options.apiKey.
    # We never include the plaintext key in the response — only IDs.
    from forge_server.db.models import UserProviderKey
    key_rows = await db.execute(
        select(UserProviderKey.provider_id).where(UserProviderKey.user_id == user_id)
    )
    keyed = sorted(set(key_rows.scalars().all()))

    _cache_put(user_id, "custom_providers", json.dumps({"providers": providers, "keyed": keyed}))
    return CustomProvidersOut(providers=providers, keyed=keyed)


# ─────────────────────────────────────────────────────────────────────────────
# Project snapshot (called by the forge-verify opencode plugin)
# ─────────────────────────────────────────────────────────────────────────────

class SnapshotOut(BaseModel):
    """Slimmed down version response — the plugin doesn't need the manifest."""
    id:          str
    project_id:  str
    created_at:  str
    is_no_op:    bool   # true when manifest matched parent (no new row)


@router.post("/projects/{project_id}/versions/snapshot", response_model=SnapshotOut)
async def snapshot_project_internal(
    project_id: str,
    _auth:      str           = Depends(verify_internal_project_token),
    db:         AsyncSession  = Depends(get_db),
):
    """Capture a new project version on behalf of a trusted internal caller.

    Used by the forge-verify opencode plugin after the primary agent's turn
    finishes cleanly. Plugin signs HMAC over `project:{project_id}:{bucket}`
    using FORGE_INTERNAL_SECRET (the same env both processes share) and
    sends it as X-Forge-Internal-Token.

    Why a dedicated internal route instead of the user-auth POST /versions:
      - Plugin runs outside any user-request context. It can't forward a
        user JWT — there isn't one.
      - The existing /agent-model uses the same HMAC pattern; this keeps
        the trust model consistent.
      - HMAC-of-project is project-scoped: a compromised token can only
        snapshot one project, not act as a user.

    No-op behavior: create_version already short-circuits when the manifest
    matches the current head. We surface that via `is_no_op` so the plugin
    can log it without confusion.
    """
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    prev_head = project.head_version_id
    try:
        v = await create_version(db, project, prompt=None, summary=None)
    except RuntimeError as e:
        # Empty workspace — should never happen post-turn, but surface clearly.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        # File-too-large guard.
        raise HTTPException(status_code=413, detail=str(e))

    # If the returned version is the same as the previous head, the no-op
    # branch fired (manifest matched). Tell the plugin.
    is_no_op = (prev_head is not None and str(v.id) == str(prev_head))

    return SnapshotOut(
        id         = str(v.id),
        project_id = str(v.project_id),
        created_at = v.created_at.isoformat(),
        is_no_op   = is_no_op,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Project runtime errors (called by the verify subagent)
# ─────────────────────────────────────────────────────────────────────────────
#
# Same trust model as snapshot above: the verify subagent runs inside opencode
# without a user JWT and authenticates via project-scoped HMAC. We mirror the
# existing user-auth GET /api/projects/{pid}/runtime-errors (which gates on
# `current_user_optional`) — same store, same shape, just different auth.

@router.get("/projects/{project_id}/runtime-errors")
async def runtime_errors_internal(
    project_id: str,
    since:      float | None  = None,
    _auth:      str           = Depends(verify_internal_project_token),
    db:         AsyncSession  = Depends(get_db),
):
    """Read the project's runtime-errors ring on behalf of the verify
    subagent. HMAC-gated — token is the project-scoped variant minted by
    forge-server's opencode_proxy and exposed to the agent shell as
    FORGE_PROJECT_TOKEN."""
    # Import locally so the heavy api module is only loaded when used.
    from forge_server.runner.runtime_errors_store import list_errors as store_list

    # Validate the project exists (defence-in-depth — HMAC verification
    # already confirms the project_id, but a stale token after a delete
    # could otherwise return [] silently and look like "no errors").
    proj = await db.scalar(select(Project).where(Project.id == project_id))
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")

    errors = await store_list(project_id, since=since)
    return {"project_id": project_id, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# Image generation (called by opencode's request_images tool)
# ─────────────────────────────────────────────────────────────────────────────
#
# Why a separate internal route instead of reusing /api/projects/{id}/images/request:
#   - The user-facing route is gated by `current_user` (JWT). opencode runs
#     as a SHARED process — there's no user JWT in env, and minting one would
#     mean creating a long-lived credential to inject into the container, with
#     all the rotation/revocation complexity that brings.
#   - Every other opencode → forge-server callback (agent-model, snapshot,
#     custom-providers) already uses the HMAC pattern with X-Forge-User-Id +
#     X-Forge-Internal-Token. Image-gen joins the same pattern instead of
#     inventing a third auth mechanism.
#   - opencode forwards the verified UserId + token it received on the
#     inbound proxy hop, so this endpoint trusts the proxy chain end-to-end.
#
# We re-use the request/response schemas + insertion logic from
# project_images_routes by importing them, so the two endpoints can't drift
# in shape (the only difference is the auth dep).

@router.post("/projects/{project_id}/images/request")
async def request_images_internal(
    project_id: str,
    body:       dict,                 # validated by the inner handler
    user_id:    str          = Depends(verify_internal_token),
    db:         AsyncSession = Depends(get_db),
):
    """Enqueue image jobs on behalf of an opencode-side request.

    Re-uses every check the user-facing route enforces (cap, dedup, mode/key
    validation) by delegating to its handler with a synthetic User object
    rebuilt from the verified user_id. Why pass a User and not a user_id
    string: the inner handler's ownership check compares
    `project.user_id == user.id`, and changing that ripples through
    project_images_routes for no benefit.
    """
    from forge_server.api.project_images_routes import (
        ImageBatchRequest, request_images as user_handler,
    )
    from forge_server.db.models import User as UserModel

    # Validate body via the same schema the user-facing route uses, so the
    # opencode tool can't accidentally evolve away from it.
    parsed = ImageBatchRequest.model_validate(body)

    # The inner handler reads only `user.id`; constructing a partial ORM
    # object avoids a DB roundtrip just to fetch the User row we don't
    # otherwise need here.
    fake_user = UserModel(id=user_id)
    return await user_handler(
        project_id = project_id,
        body       = parsed,
        user       = fake_user,
        db         = db,
    )
