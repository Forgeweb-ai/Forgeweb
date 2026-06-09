"""
forge_server/api/provider_routes.py
=====================================
Per-user encrypted API key management for LLM providers.

Endpoints:
  GET    /api/user/providers          — list providers the user has keys for
  POST   /api/user/providers          — add or update a provider key (encrypted at rest)
  DELETE /api/user/providers/{id}     — remove a provider key

Phase 2 (current): keys live ONLY in `user_provider_keys.key_enc` (Fernet at
rest). The opencode runtime gets per-call decrypted keys via the
`X-Forge-Auth` header injected by `runner/opencode_proxy.py`; opencode reads
them through the request-scoped `Auth.Override` Context.Reference and never
touches disk. See `services/key_cache.py` for the in-memory decrypted-key
cache that backs the proxy.

Encryption: same Fernet key used for project env vars.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user
from forge_server.api.config_routes import _fernet          # reuse same key
from forge_server.db.database import get_db
from forge_server.db.models import User, UserProviderKey
from forge_server.services.key_cache import cache

log    = logging.getLogger("forge.providers")
router = APIRouter(prefix="/api/user/providers", tags=["provider-keys"])


# ── Encryption helpers (same Fernet as config_routes) ────────────────────────

def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


# ── Schemas ───────────────────────────────────────────────────────────────────

class ProviderKeyIn(BaseModel):
    provider_id: str       # e.g. "anthropic", "openai", "moonshot"
    api_key:     str       # plaintext — never stored, encrypted immediately
    label:       str | None = None

class ProviderKeyOut(BaseModel):
    id:          str
    provider_id: str
    label:       str | None
    created_at:  datetime
    updated_at:  datetime


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[ProviderKeyOut])
async def list_provider_keys(
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """List which providers the user has keys for. API key values are never returned."""
    result = await db.execute(
        select(UserProviderKey)
        .where(UserProviderKey.user_id == user.id)
        .order_by(UserProviderKey.provider_id)
    )
    rows = result.scalars().all()
    return [
        ProviderKeyOut(
            id=r.id, provider_id=r.provider_id,
            label=r.label, created_at=r.created_at, updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.post("", response_model=ProviderKeyOut, status_code=201)
async def set_provider_key(
    body: ProviderKeyIn,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Add or update an API key for a provider. Encrypted before storing.
    The in-memory key cache is invalidated so the next proxied opencode call
    re-reads + re-decrypts; opencode itself picks up the change on the next
    request without any restart.
    """
    provider_id = body.provider_id.strip().lower()
    if not provider_id:
        raise HTTPException(status_code=422, detail="provider_id is required")
    if not body.api_key.strip():
        raise HTTPException(status_code=422, detail="api_key is required")

    encrypted = _encrypt(body.api_key.strip())

    # Upsert
    result = await db.execute(
        select(UserProviderKey).where(
            UserProviderKey.user_id    == user.id,
            UserProviderKey.provider_id == provider_id,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.key_enc    = encrypted
        row.label      = body.label
        row.updated_at = datetime.utcnow()
    else:
        row = UserProviderKey(
            user_id=user.id, provider_id=provider_id,
            key_enc=encrypted, label=body.label,
        )
        db.add(row)

    await db.flush()
    await db.commit()
    await db.refresh(row)

    # Drop the cached decrypted map for this user so the next request reloads.
    cache().invalidate(str(user.id))
    # Also bust the opencode-facing resolver cache: the `keyed` field inside
    # the custom-providers payload depends on which providers the user has
    # stored keys for, so adding/updating a key changes that response.
    # Without this, a user-custom provider appears in the dropdown but
    # greyed out as "not connected" for up to 60s after the key is saved.
    from forge_server.api.internal_routes import invalidate_user_setting_cache
    invalidate_user_setting_cache(str(user.id))

    log.info("User %s set key for provider %s", user.id, provider_id)
    return ProviderKeyOut(
        id=row.id, provider_id=row.provider_id,
        label=row.label, created_at=row.created_at, updated_at=row.updated_at,
    )


@router.delete("/{provider_id}", status_code=204)
async def delete_provider_key(
    provider_id: str,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Remove a provider key and invalidate the user's cached key map."""
    provider_id = provider_id.strip().lower()
    result = await db.execute(
        select(UserProviderKey).where(
            UserProviderKey.user_id    == user.id,
            UserProviderKey.provider_id == provider_id,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"No key stored for provider '{provider_id}'")

    await db.delete(row)
    await db.flush()
    await db.commit()

    cache().invalidate(str(user.id))
    # Same reason as set_provider_key: deleting a key flips `keyed` for the
    # opencode-facing payload, so the "connected" badge updates immediately
    # instead of lagging up to 60s.
    from forge_server.api.internal_routes import invalidate_user_setting_cache
    invalidate_user_setting_cache(str(user.id))

    log.info("User %s deleted key for provider %s", user.id, provider_id)
