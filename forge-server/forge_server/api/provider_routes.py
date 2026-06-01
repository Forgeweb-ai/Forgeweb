"""
forge_server/api/provider_routes.py
=====================================
Per-user encrypted API key management for LLM providers.

Endpoints:
  GET    /api/user/providers          — list providers the user has keys for
  POST   /api/user/providers          — add or update a provider key (encrypted at rest)
  DELETE /api/user/providers/{id}     — remove a provider key

After every write the decrypted keys are flushed to opencode's auth.json
($XDG_DATA_HOME/opencode/auth.json) so opencode picks them up immediately
without a restart — it reads auth.json fresh on every API call.

Encryption: same Fernet key used for project env vars.

──────────────────────────────────────────────────────────────────────────────
KNOWN ISSUE — multi-tenant key leak (intentionally deferred)
──────────────────────────────────────────────────────────────────────────────
The shared opencode process reads from a single $XDG_DATA_HOME/opencode/auth.json,
which means `_flush_auth_json` here is last-write-wins ACROSS USERS. At 100k
containers this is both a security leak (cross-user key reuse) and a scale
dead-end.

Phase 2 fix (separate change set):
  1. Patch the vendored opencode/ fork so each API call carries
     {provider_id, key, model} resolved from forge-server using the calling
     user's JWT — keys stay encrypted in DB until in-memory at the boundary,
     and never touch disk.
  2. Once Phase 2 lands: delete `_flush_auth_json`, `_auth_json_path`, and
     the auth.json writes below. Keys live ONLY in user_provider_keys.key_enc.

Until then `_flush_auth_json` remains as the explicit stopgap so the dev loop
"I set a key in Settings, opencode uses it" keeps working.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from base64 import urlsafe_b64encode
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user
from forge_server.api.config_routes import _fernet          # reuse same key
from forge_server.db.database import get_db
from forge_server.db.models import User, UserProviderKey

log    = logging.getLogger("forge.providers")
router = APIRouter(prefix="/api/user/providers", tags=["provider-keys"])

# ── opencode auth.json path ───────────────────────────────────────────────────
# Mirrors Global.Path.data in opencode/packages/core/src/global.ts.
# On macOS: ~/Library/Application Support/opencode/auth.json
# On Linux: ~/.local/share/opencode/auth.json
# Overridable via OPENCODE_AUTH_JSON env var for Docker / test environments.

def _auth_json_path() -> Path:
    override = os.environ.get("OPENCODE_AUTH_JSON")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "opencode" / "auth.json"
    home = Path.home()
    # macOS default (matches xdg-basedir on macOS)
    mac = home / "Library" / "Application Support" / "opencode" / "auth.json"
    if mac.parent.exists() or os.uname().sysname == "Darwin":
        return mac
    # Linux / other XDG default
    return home / ".local" / "share" / "opencode" / "auth.json"


# ── Encryption helpers (same Fernet as config_routes) ────────────────────────

def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()

def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


# ── Write decrypted keys → opencode auth.json ─────────────────────────────────

def _flush_auth_json(keys: list[UserProviderKey]) -> None:
    """
    Re-write opencode's auth.json with all of the user's provider keys.
    Format matches opencode's Auth.Api schema:
      { "<providerID>": { "type": "api", "key": "<plaintext>" }, ... }
    """
    path = _auth_json_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing auth.json so we don't clobber OAuth tokens or other entries
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except Exception:
                existing = {}

        # Overlay our provider keys (user-managed keys take precedence)
        for k in keys:
            try:
                plaintext = _decrypt(k.key_enc)
                existing[k.provider_id] = {"type": "api", "key": plaintext}
            except Exception:
                log.warning("Could not decrypt key for provider %s", k.provider_id)

        path.write_text(json.dumps(existing, indent=2))
        # Restrict to owner-read/write only (matches opencode's own 0o600 writes)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        log.info("Flushed %d provider key(s) to %s", len(keys), path)
    except Exception as exc:
        log.error("Failed to write auth.json: %s", exc)


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
    Add or update an API key for a provider.
    The key is encrypted before storing. opencode's auth.json is updated
    immediately so the running opencode process picks it up without restart.
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

    # Flush all this user's keys to auth.json
    all_keys_result = await db.execute(
        select(UserProviderKey).where(UserProviderKey.user_id == user.id)
    )
    _flush_auth_json(list(all_keys_result.scalars().all()))

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
    """Remove a provider key and update auth.json."""
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

    # Flush remaining keys (without the deleted one)
    all_keys_result = await db.execute(
        select(UserProviderKey).where(UserProviderKey.user_id == user.id)
    )
    _flush_auth_json(list(all_keys_result.scalars().all()))

    log.info("User %s deleted key for provider %s", user.id, provider_id)
