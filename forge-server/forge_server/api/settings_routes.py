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

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from forge_server.api.auth import current_user
from forge_server.api.constants import SETTINGS_DEFAULTS
from forge_server.db.database import get_db
from forge_server.db.models import User, UserSettings

log    = logging.getLogger("forge.settings")
router = APIRouter(prefix="/api/user", tags=["user-settings"])

# Re-export so existing callers can still do `from settings_routes import DEFAULTS`
DEFAULTS = SETTINGS_DEFAULTS


# ── Schemas ───────────────────────────────────────────────────────────────────

class SettingsPatch(BaseModel):
    # All fields optional — PATCH semantics: omitted = untouched.
    primary_model: str | None = None
    design_model:  str | None = None


class SettingsOut(BaseModel):
    primary_model: str
    design_model:  str


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_or_create(user: User, db: AsyncSession) -> UserSettings:
    result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    row = result.scalar_one_or_none()
    if not row:
        row = UserSettings(user_id=user.id, settings_json=json.dumps(DEFAULTS))
        db.add(row)
        await db.flush()
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
    row  = await _get_or_create(user, db)
    data = {**DEFAULTS, **json.loads(row.settings_json or "{}")}

    if body.primary_model is not None:
        data["primary_model"] = body.primary_model
    if body.design_model is not None:
        data["design_model"] = body.design_model

    row.settings_json = json.dumps(data)
    row.updated_at    = datetime.utcnow()
    await db.commit()

    log.info("User %s updated settings: %s", user.id, body.model_dump(exclude_none=True))
    return SettingsOut(**data)
