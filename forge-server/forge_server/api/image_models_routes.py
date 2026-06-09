"""
forge_server/api/image_models_routes.py
=========================================
Read-only endpoint exposing the curated image-model catalog, filtered to
entries the user can actually call.

Why a separate endpoint instead of reusing /api/user/providers or the LLM
model list:
  - LLM model list (`useModels()` on the FE) is opencode-shaped (provider
    SDKs, modalities, tool_call, attachment). Image providers don't fit that
    shape and don't share an SDK. Mixing them creates capability-confusion in
    the UI — users picking DALL·E from a "models" list and getting unhelpful
    errors when the LLM agent dispatches them.
  - The catalog is filtered SERVER-SIDE by the user's connected keys so the
    FE never has to know the provider→key mapping. Adding a new model =
    one entry in providers.py, no FE deploy.

Cost shape: one DB read (small, indexed) per call. The Settings page hits
this once per dialog open; main app does not call it. Flat per user.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import json

from forge_server.api.auth import current_user
from forge_server.db.database import get_db
from forge_server.db.models import User, UserProviderKey, UserSettings
from forge_server.imagegen.providers import IMAGE_MODELS, available_for, custom_entry_from_dict

log    = logging.getLogger("forge.image_models")
router = APIRouter(prefix="/api/image-models", tags=["image-models"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ImageModelOut(BaseModel):
    """One row in the response. Mirrors providers.ImageModel but trimmed to
    fields the FE picker actually needs.
    """
    # The composite id the FE saves to user_settings.image_model.
    # Stable across renames of display_name so existing user choices keep
    # resolving even if marketing copy changes.
    id:                    str   # "<provider_id>/<model_id>"
    provider_id:           str
    model_id:              str
    display_name:          str
    required_key_provider: str
    protocol:              str
    supports_img2img:      bool
    sizes:                 list[str]
    price_usd_per_image:   float
    description:           str
    base_url:              str | None = None
    # `unlocked` is the load-bearing field for the Settings UI:
    #   true  → user has the required key, model selectable
    #   false → render disabled with a "Connect <provider> key" hint
    # We send the WHOLE catalog (unlocked + locked) so the user discovers
    # what's available before committing to a new key — a filtered-only
    # response would hide the upsell. Token cost is negligible (~7 entries).
    unlocked:              bool
    # Distinguish curated registry entries (immutable, vetted) from
    # user-defined ones (editable in Settings → Image AI → Custom). FE
    # renders an "Edit / Remove" affordance only for source="custom".
    source:                str   # "builtin" | "custom"


class ImageModelsResponse(BaseModel):
    models:                list[ImageModelOut]
    # Echo back what we matched against so the FE can render a clear
    # "you have keys for: X, Y" hint without a second round-trip.
    connected_key_providers: list[str]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _connected_providers(user: User, db: AsyncSession) -> set[str]:
    """Return the lowercase set of provider_ids the user has a key row for.

    Lowercased so we compare against provider_id values regardless of how
    the FE persisted them (we accept mixed case on POST but want
    case-insensitive matching downstream).
    """
    result = await db.execute(
        select(UserProviderKey.provider_id).where(UserProviderKey.user_id == user.id)
    )
    return {row[0].lower() for row in result.all()}


# ── Routes ────────────────────────────────────────────────────────────────────

async def _custom_entries(user: User, db: AsyncSession) -> list:
    """Hydrate this user's custom_image_providers map into ImageModel objects.

    Bad rows are silently skipped (the hydrator returns None) — we don't
    want one malformed entry from an old migration to break the whole
    Settings dialog. The PATCH endpoint rejects malformed entries up front,
    so this is a belt-and-braces filter.
    """
    result = await db.execute(
        select(UserSettings.settings_json).where(UserSettings.user_id == user.id)
    )
    raw = result.scalar_one_or_none()
    if not raw:
        return []
    try:
        custom_map = (json.loads(raw) or {}).get("custom_image_providers") or {}
    except Exception:
        return []
    out = []
    for _key, cfg in custom_map.items():
        entry = custom_entry_from_dict(cfg)
        if entry is not None:
            out.append(entry)
    return out


def _to_out(m, unlocked: bool, source: str) -> ImageModelOut:
    """Single materialization helper used for both built-in and custom rows."""
    return ImageModelOut(
        id                     = f"{m.provider_id}/{m.model_id}",
        provider_id            = m.provider_id,
        model_id               = m.model_id,
        display_name           = m.display_name,
        required_key_provider  = m.required_key_provider,
        protocol               = m.protocol,
        supports_img2img       = m.supports_img2img,
        sizes                  = list(m.sizes),
        price_usd_per_image    = m.price_usd_per_image,
        description            = m.description,
        base_url               = m.base_url,
        unlocked               = unlocked,
        source                 = source,
    )


@router.get("", response_model=ImageModelsResponse)
async def list_image_models(
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Return curated registry + user's custom entries, with per-row
    `unlocked` flag and `source` discriminator for the FE picker.
    """
    connected = await _connected_providers(user, db)
    models: list[ImageModelOut] = []

    # Built-in registry first — these are the well-known, vetted entries.
    for m in IMAGE_MODELS:
        models.append(_to_out(
            m,
            unlocked = m.required_key_provider.lower() in connected,
            source   = "builtin",
        ))

    # User's custom entries appended after. Order within custom matches the
    # dict iteration order which on modern Python is insertion order — the FE
    # can re-sort if it wants but we don't impose a default.
    for m in await _custom_entries(user, db):
        models.append(_to_out(
            m,
            unlocked = m.required_key_provider.lower() in connected,
            source   = "custom",
        ))

    return ImageModelsResponse(
        models                  = models,
        connected_key_providers = sorted(connected),
    )
