"""
forge_server/api/settings_routes.py
=====================================
Per-user settings API.

Endpoints:
  GET   /api/user/settings   — read current user settings (design_model, etc.)
  PATCH /api/user/settings   — update one or more settings fields

Settings are stored as a JSON blob in user_settings.settings_json.
Defaults are applied in the GET response so the FE always gets complete data.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from forge_server.api.auth import current_user
from forge_server.api.constants import (
    IMAGE_MODE_VALUES,
    MAX_ENABLED_MODELS,
    PLATFORM_PROVIDER_IDS,
    SETTINGS_DEFAULTS,
)
from forge_server.db.database import get_db
from forge_server.db.models import User, UserSettings

log    = logging.getLogger("forge.settings")
router = APIRouter(prefix="/api/user", tags=["user-settings"])

# Re-export so existing callers can still do `from settings_routes import DEFAULTS`
DEFAULTS = SETTINGS_DEFAULTS


# ── Schemas ───────────────────────────────────────────────────────────────────

class CustomProviderUpsert(BaseModel):
    """A single custom-provider definition the FE sends on PATCH.

    Mirrors the opencode provider shape (name/npm/options/models/headers)
    but with no `apiKey` field — keys go through /api/user/providers and
    are stored encrypted in user_provider_keys.key_enc. Mixing the two
    storage paths is the BYOK foot-gun we're closing here.
    """
    name:    str | None = None
    npm:     str | None = None
    # `options` is opaque to forge-server (baseURL, headers passthrough,
    # etc.); opencode owns the schema. Keeping it as a plain dict lets
    # opencode evolve fields without forcing a forge-server release.
    options: dict[str, Any] | None = None
    models:  dict[str, Any] | None = None
    headers: dict[str, Any] | None = None


class CustomImageProviderUpsert(BaseModel):
    """One user-defined image AI entry. Same field set as imagegen.providers
    .ImageModel — we re-declare here (rather than importing) so the
    settings module stays decoupled from the imagegen subsystem (handy for
    independent deploys).
    """
    provider_id:           str
    model_id:              str
    display_name:          str
    required_key_provider: str
    protocol:              str
    # base_url is optional for protocols with a fixed URL (replicate) and
    # required for protocols that take one (openrouter_chat, openai_images).
    # We don't enforce this at schema level — the imagegen registry's
    # hydrator silently drops malformed entries, and the worker will mark
    # the job failed with a clean category if a needed URL is missing.
    base_url:              str | None = None
    supports_img2img:      bool = False
    sizes:                 list[str] | None = None
    price_usd_per_image:   float = 0.0
    description:           str = "Custom AI provider."


class SettingsPatch(BaseModel):
    # All fields optional — PATCH semantics: omitted = untouched.
    primary_model:    str | None = None
    design_model:     str | None = None
    # image_model: "<provider>/<model>" from the curated image registry OR
    # from user's custom_image_providers, or "" to clear (turns the feature
    # OFF regardless of image_mode). Unlike primary/design which never
    # legitimately receive "", an empty image_model is a meaningful
    # "disable" signal, so we accept it.
    image_model:      str | None = None
    # image_mode: "off" | "auto" | "ask". Validated against IMAGE_MODE_VALUES;
    # anything else → 422.
    image_mode:       str | None = None
    # Whole-map replace for custom_providers — the FE sends the desired
    # state. Set to {} to clear. We could expose per-provider PATCH later
    # but the dialog only deals in whole records and this keeps the API
    # symmetrical with how the value is read.
    custom_providers: dict[str, CustomProviderUpsert] | None = None
    # Whole-map replace for custom_image_providers. Keyed by
    # "<provider_id>/<model_id>" (same shape as user_settings.image_model).
    custom_image_providers: dict[str, CustomImageProviderUpsert] | None = None
    # Whole-list replace for the model-visibility allowlist. Each entry is
    # "<providerID>/<modelID>". None = leave alone (PATCH semantics); []
    # explicitly clears the allowlist (FE falls back to default policy).
    # Symmetric with custom_providers — FE owns the desired state, BE just
    # stores it. See SETTINGS_DEFAULTS["enabled_models"] for semantics.
    enabled_models: list[str] | None = None


class SettingsOut(BaseModel):
    primary_model:    str
    design_model:     str
    image_model:      str
    image_mode:       str
    # Raw dict on the way out — same shape the opencode resolver consumes.
    custom_providers:       dict[str, dict[str, Any]] = {}
    custom_image_providers: dict[str, dict[str, Any]] = {}
    # Model-visibility allowlist; see SETTINGS_DEFAULTS for semantics.
    enabled_models:         list[str] = []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bad_base_url(value: str) -> str | None:
    """Return a short rejection reason for an unsafe custom-AI base_url,
    or None when the URL passes the textual checks.

    This is intentionally a STRING check, not a DNS resolve. DNS resolution
    here would:
      (a) cost a per-PATCH network roundtrip (bad at scale),
      (b) be race-prone (TOCTOU — resolves clean here, points at private
          IP by the time the worker calls it).
    The worker should re-check at call time. This function is the "obviously
    wrong" gate that catches the common cases (typed-in localhost / private
    range / non-https scheme) without that overhead.
    """
    import ipaddress
    from urllib.parse import urlparse

    try:
        parsed = urlparse(value.strip())
    except Exception:
        return "could not parse URL"

    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        # Even `http://` is rejected — image-gen providers all serve over
        # https in 2026, and downgrading would leak the BYOK key on the wire.
        return f"only https:// is allowed (got {scheme!r})"

    host = (parsed.hostname or "").lower()
    if not host:
        return "missing host"

    # Block raw private-range IPv4/IPv6 literals. Hostnames that resolve
    # to private ranges still pass this check — the worker layer is the
    # backstop for the DNS-time check.
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            return f"private/loopback IPs are not allowed ({host})"
    except ValueError:
        # Not an IP literal — fine, fall through to hostname check.
        pass

    # Common hostname-based footguns. These are not exhaustive (a determined
    # user can still aim at internal hosts via DNS) but catch the
    # accidental cases.
    BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata"}
    if host in BLOCKED_HOSTNAMES:
        return f"hostname {host!r} is not allowed"
    if host.endswith(".internal") or host.endswith(".local"):
        return f"internal hostnames are not allowed ({host})"

    return None


async def _get_or_create(
    user: User,
    db:   AsyncSession,
    *,
    lock: bool = False,
) -> UserSettings:
    """Load the user's settings row, optionally taking a row-level lock.

    `lock=True` (PATCH path) issues `SELECT ... FOR UPDATE` so concurrent
    writers serialize on this row. settings_json is read-modify-write of a
    single JSON blob, so without a lock two concurrent PATCHes (e.g. a
    user-initiated design_model change AND the FE's debounced
    enabled_models save) race: both load the same snapshot, both write
    back their merged copy, and the second commit silently clobbers the
    field the first one changed. Postgres' default READ COMMITTED
    isolation allows this — the lock turns the second writer into a wait
    instead of an overwrite. The lock is released on commit/rollback,
    which is per-request here.

    `lock=False` (GET path) skips the lock — readers don't block readers,
    and a GET that races a concurrent write just returns whichever
    snapshot is committed at read time, which is fine.
    """
    stmt = select(UserSettings).where(UserSettings.user_id == user.id)
    if lock:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        row = UserSettings(user_id=user.id, settings_json=json.dumps(DEFAULTS))
        db.add(row)
        await db.flush()
        if lock:
            # The freshly-inserted row isn't locked yet; re-fetch under the
            # lock so any concurrent PATCH that lost the insert-race still
            # serializes against us. Cheap (single-row PK lookup) and only
            # runs on the very first PATCH per user.
            result = await db.execute(stmt)
            locked = result.scalar_one_or_none()
            if locked is not None:
                row = locked
    return row


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=SettingsOut)
async def get_settings(
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Return the current user's settings with defaults filled in."""
    row  = await _get_or_create(user, db)
    data = {**DEFAULTS, **json.loads(row.settings_json or "{}")}
    await db.commit()
    return SettingsOut(**data)


@router.patch("/settings", response_model=SettingsOut)
async def patch_settings(
    body: SettingsPatch,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Update one or more settings fields."""
    # lock=True: serialize concurrent PATCHes so we never lose a write to a
    # different field via stale read-modify-write. See _get_or_create docs.
    row  = await _get_or_create(user, db, lock=True)
    data = {**DEFAULTS, **json.loads(row.settings_json or "{}")}

    # Treat empty-string as "no change" rather than overwriting with empty.
    # Closes a real bug: a FE component that initialised its <select> with
    # value="" and fired a PATCH carrying `design_model: ""` would wipe the
    # user's saved choice and force the next GET to fall back to the default
    # — the user then sees "the setting keeps resetting itself." PATCH
    # semantics here are "set if provided AND meaningful"; omit or send null
    # to leave a field alone. There's no legitimate reason to PATCH either
    # model field to "".
    if body.primary_model:
        data["primary_model"] = body.primary_model
    if body.design_model:
        data["design_model"] = body.design_model

    # image_model: empty string IS meaningful here (it disables the feature)
    # — distinct from omitted/None which means "leave alone". A FE that does
    # NOT support image-gen yet will never send this field, so existing rows
    # remain untouched. The default falls through from SETTINGS_DEFAULTS.
    if body.image_model is not None:
        data["image_model"] = body.image_model
    if body.image_mode is not None:
        if body.image_mode not in IMAGE_MODE_VALUES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"image_mode must be one of {sorted(IMAGE_MODE_VALUES)}; "
                    f"got {body.image_mode!r}."
                ),
            )
        data["image_mode"] = body.image_mode

    if body.custom_image_providers is not None:
        # Validate each entry against the imagegen registry's hydrator so the
        # FE can't persist a config the worker will silently drop later.
        # Lazy import — keeps settings_routes importable in test contexts
        # that don't need the imagegen subsystem.
        from forge_server.imagegen.providers import PROTOCOLS, custom_entry_from_dict
        bad_keys: list[str] = []
        out: dict[str, dict[str, Any]] = {}
        for key, cfg in body.custom_image_providers.items():
            # The map key MUST equal "<provider_id>/<model_id>" so
            # user_settings.image_model can index into it directly. Reject
            # mismatches up front rather than letting the worker hit a
            # phantom entry.
            expected = f"{cfg.provider_id}/{cfg.model_id}"
            if key != expected:
                bad_keys.append(f"{key} (expected {expected})")
                continue
            if cfg.protocol not in PROTOCOLS:
                bad_keys.append(f"{key} (unknown protocol {cfg.protocol!r})")
                continue
            # SSRF guard on base_url. Without this, a user could point a custom
            # entry at a private-network endpoint and have the image worker
            # (which runs inside our infrastructure) fetch from it. Block:
            #   - any non-https scheme (no http://, no file://, no ftp://, …)
            #   - hostnames that resolve to loopback/private/link-local ranges
            #   - bare ips in any private range
            # We do NOT do DNS resolution here — that costs a roundtrip per
            # PATCH and can be raced via TOCTOU. Reject only the textual
            # patterns; the worker also re-validates at call time as a
            # belt-and-braces defense (see follow-up note in worker.py).
            if cfg.base_url is not None and cfg.base_url != "":
                bad = _bad_base_url(cfg.base_url)
                if bad:
                    bad_keys.append(f"{key} (base_url: {bad})")
                    continue
            payload = cfg.model_dump(exclude_none=True)
            # Hydration round-trip catches type/coercion edge cases the
            # pydantic schema can't (e.g. an empty sizes list).
            if custom_entry_from_dict(payload) is None:
                bad_keys.append(f"{key} (failed hydration)")
                continue
            out[key] = payload
        if bad_keys:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid custom_image_providers entries: {', '.join(bad_keys)}",
            )
        data["custom_image_providers"] = out

    if body.enabled_models is not None:
        # Validate each entry:
        #   - non-empty string
        #   - exactly one "/" separator (providerID/modelID)
        #   - neither side empty
        # We deliberately do NOT cross-check against the live provider/model
        # registry here. The registry is dynamic (depends on which keys the
        # user has on file, which provider catalogs are reachable, etc.);
        # rejecting an entry the user has just toggled because the catalog
        # is momentarily unreachable would cause spurious save failures.
        # The FE resolver in models.tsx tolerates entries for currently-
        # unknown models — they simply contribute nothing until the catalog
        # reappears. Same tolerance pattern as the design_model "(saved)"
        # synthesized option in dialog-manage-models.tsx.
        if len(body.enabled_models) > MAX_ENABLED_MODELS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"enabled_models exceeds cap of {MAX_ENABLED_MODELS} "
                    f"entries (got {len(body.enabled_models)})."
                ),
            )
        bad_entries: list[str] = []
        seen: set[str] = set()
        cleaned: list[str] = []
        for entry in body.enabled_models:
            if not isinstance(entry, str) or not entry.strip():
                bad_entries.append(repr(entry))
                continue
            parts = entry.split("/", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                bad_entries.append(entry)
                continue
            if entry in seen:
                continue  # silent dedupe — order preserved
            seen.add(entry)
            cleaned.append(entry)
        if bad_entries:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"enabled_models entries must match '<providerID>/<modelID>'. "
                    f"Invalid: {', '.join(bad_entries[:10])}"
                    + ("…" if len(bad_entries) > 10 else "")
                ),
            )
        data["enabled_models"] = cleaned

    if body.custom_providers is not None:
        # Platform-collision guard. A user redefining `anthropic` here would
        # bypass forge-llm-proxy (cost accounting, sanitization, rate-card
        # lookup), so reject the whole PATCH up-front. Error message points
        # at the right BYOK flow so the user knows what to do — the previous
        # "Cannot redefine platform-owned provider(s)" surfaced as a generic
        # 422 the FE displayed unchanged ("Request failed") with no path
        # forward. We surface bad IDs together so the FE shows them all at
        # once instead of erroring one-by-one. The FE has its own
        # PLATFORM_PROVIDER_IDS check that catches this before submit; this
        # backend guard is the load-bearing one (FE could be bypassed).
        bad = sorted(pid for pid in body.custom_providers if pid.lower() in PLATFORM_PROVIDER_IDS)
        if bad:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{', '.join(bad)} is built-in. To use your own API key, "
                    f"save it under Settings → Providers → {bad[0]} (the BYOK "
                    f"key flow). Custom-provider registration is only for "
                    f"providers Forge doesn't already support."
                ),
            )
        # Dump each upsert via model_dump so we store plain JSON shapes,
        # not pydantic objects (the column is JSON).
        data["custom_providers"] = {
            pid: cfg.model_dump(exclude_none=True)
            for pid, cfg in body.custom_providers.items()
        }

    row.settings_json = json.dumps(data)
    row.updated_at    = datetime.utcnow()
    await db.commit()

    # Bust the opencode-facing resolver cache so the user's save is visible
    # on the very next /api/internal/* call instead of waiting up to 60s for
    # the TTL to expire. This is what makes "save → see provider in the
    # dropdown" feel synchronous. Lazy import to avoid an import cycle
    # (internal_routes imports things settings_routes already brings in).
    from forge_server.api.internal_routes import invalidate_user_setting_cache
    invalidate_user_setting_cache(str(user.id))

    log.info("User %s updated settings: %s", user.id, body.model_dump(exclude_none=True))
    return SettingsOut(**data)
