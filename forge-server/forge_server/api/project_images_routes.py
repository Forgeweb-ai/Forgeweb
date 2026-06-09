"""
forge_server/api/project_images_routes.py
===========================================
HTTP surface for the per-project image-gen queue.

Endpoints (all under /api/projects/{project_id}/images):

  POST   ↦ enqueue a BATCH of image jobs. Called by the opencode
           `request_images` tool inside the agent's turn — body carries one
           or more {slot_id, prompt, size, ref?} requests, response carries
           the slot ids the agent should embed in JSX as
           `<img src="/forge-img/{slot_id}.png">`. NON-BLOCKING — returns
           immediately; the image worker drains the queue out of band.
  GET    ↦ list jobs for this project, newest first; optional `since` and
           `status` filters. Used by:
             - FE status pill ("Images: 2/3 ready") via polling
             - runner /forge-img/{slot_id} resolver (single-row lookup,
               below)
  GET /{slot_id} ↦ single-row lookup keyed by slot_id. The runner calls
           this to resolve `<img src=/forge-img/{slot_id}>` requests.

Hard rules (TODO_IMAGE_GEN.md §"Hard rules"):
  - **Per-request cap of 6.** If the agent asks for 7+ in one POST, we
    accept the first 6 and reject the rest with a clear note in the
    response. We do NOT silently truncate — the agent needs to know so it
    can rewrite the JSX to not reference dropped slots.
  - **Dedup by hash(provider:model:size:prompt:ref_sha).** Second POST
    with the same hash short-circuits to the existing job's row, returning
    the same slot id — saves the user a billed call AND keeps the JSX
    pointing at the asset that's already in flight.
  - **Image-gen must be turned ON in user_settings** (`image_mode != "off"`
    and `image_model` is set). If not, this endpoint returns 409 with a
    clear hint — the agent shouldn't have called it.

Auth: project owner only. (No iframe-bridge unauth path here; the iframe
never enqueues image jobs.)
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user
from forge_server.api.constants import SETTINGS_DEFAULTS
from forge_server.db.database import get_db
from forge_server.db.models import ImageJob, Project, User, UserSettings
from forge_server.imagegen import providers as registry

log    = logging.getLogger("forge.project_images")
router = APIRouter(prefix="/api/projects", tags=["project-images"])


# ── Constants ────────────────────────────────────────────────────────────────

# Per-turn hard cap. Matches TODO_IMAGE_GEN.md §"Hard rules". Anything above
# this gets rejected — the agent needs to budget within the cap, not have
# the platform silently drop requests it thought succeeded.
MAX_IMAGES_PER_REQUEST = 6

# slot_id alphabet: URL-safe, no padding, no easily-confused chars (no
# zero/one/uppercase-O/uppercase-I). Length 16 → 32^16 ≈ 1.2e24 namespace
# so the runner's public /forge-img/{slot_id} route can resolve globally
# without a project_id in the URL: collisions are astronomically rare AND
# unguessable from a leaked slot id. (Per-project unique constraint still
# catches the once-in-a-quadrillion intra-project collision.)
_SLOT_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def _gen_slot_id() -> str:
    return "".join(secrets.choice(_SLOT_ALPHABET) for _ in range(16))


def _dedup_hash(provider_id: str, model_id: str, size: str, prompt: str, ref_sha: str | None) -> str:
    """Stable hash that lets us short-circuit duplicate requests.

    Including model + size + ref means a same-prompt request at a different
    size/model/ref reuses NOTHING (correct — they'd produce different
    images). Same-everything → reuse.
    """
    payload = f"{provider_id}|{model_id}|{size}|{prompt}|{ref_sha or ''}"
    return hashlib.sha256(payload.encode()).hexdigest()


# ── Schemas ──────────────────────────────────────────────────────────────────

class ImageRequestItem(BaseModel):
    """One image the agent wants generated."""
    prompt: str            = Field(..., min_length=1, max_length=4000)
    # Size hint. Adapter snaps to nearest supported size per the registry —
    # an invalid size falls back to the registry's default rather than
    # rejecting the whole batch.
    size:   str | None     = Field(default=None, max_length=16)
    # Reference image, content-addressed by sha256 if it's already in the
    # blob store. NULL = pure txt2img. We accept the sha here (not raw
    # bytes) so the request body stays small and the agent can pre-upload
    # references via the blob store (v1.1) without re-sending them per turn.
    ref_blob_sha: str | None = Field(default=None, max_length=64)


class ImageBatchRequest(BaseModel):
    items: list[ImageRequestItem] = Field(..., min_length=1, max_length=MAX_IMAGES_PER_REQUEST + 10)


class ImageJobOut(BaseModel):
    """Returned for every enqueued / looked-up job."""
    slot_id:     str
    # The URL the agent should embed in JSX. Stable for the slot's lifetime.
    served_url:  str    # "/forge-img/{slot_id}.png"
    status:      str    # queued | running | done | failed
    provider_id: str
    model_id:    str
    size:        str
    prompt:      str
    # Populated once the worker finishes; null while pending.
    output_url:  str | None = None
    error:       str | None = None
    created_at:  datetime
    completed_at: datetime | None = None
    # `deduplicated` is true when this slot was returned from an existing
    # job hash-match rather than freshly inserted. Lets the agent log
    # "reused N of M" instead of confidently reporting all as new.
    deduplicated: bool = False


class ImageBatchResponse(BaseModel):
    jobs:    list[ImageJobOut]
    # Anything dropped by the per-request cap. Empty when the agent stayed
    # within budget.
    rejected: list[dict[str, Any]] = []


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _owned_project(project_id: str, user: User, db: AsyncSession) -> Project:
    p = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Project not found")
    if p.user_id != user.id:
        raise HTTPException(403, "Forbidden")
    return p


async def _user_image_config(user: User, db: AsyncSession) -> tuple[str, str, dict]:
    """Return (image_model, image_mode, custom_image_providers).

    Falls back to SETTINGS_DEFAULTS so a fresh user without any
    user_settings row at all still gets a sane "off" answer rather than a
    500.
    """
    row = (await db.execute(
        select(UserSettings.settings_json).where(UserSettings.user_id == user.id)
    )).scalar_one_or_none()
    data = {**SETTINGS_DEFAULTS}
    if row:
        try:
            data.update(json.loads(row) or {})
        except Exception:
            pass
    return (
        str(data.get("image_model") or ""),
        str(data.get("image_mode")  or "off"),
        dict(data.get("custom_image_providers") or {}),
    )


def _resolve_registry_entry(image_model: str, custom_map: dict) -> registry.ImageModel | None:
    """Resolve `<provider>/<model>` against built-ins first, then custom."""
    parsed = registry.parse_settings_value(image_model)
    if parsed is None:
        return None
    pid, mid = parsed
    entry = registry.lookup(pid, mid)
    if entry is not None:
        return entry
    cfg = custom_map.get(f"{pid}/{mid}")
    if cfg is not None:
        return registry.custom_entry_from_dict(cfg)
    return None


def _to_out(j: ImageJob, *, deduplicated: bool = False) -> ImageJobOut:
    # Post-refactor (2026-06-04): served_url is a plain relative path
    # `/images/<slot>.png`. The agent embeds it verbatim in JSX; the
    # project's own dev server resolves it against the preview origin
    # (the iframe IS the project domain), so no host bridging is needed.
    #
    # While a job is queued/running, the file does not yet exist on disk —
    # the iframe will show a broken-image flash for ~2-5s until the worker
    # writes the bytes. Acceptable v1 tradeoff vs. the four bugs the old
    # /forge-img route generated. A real shimmer placeholder can land as
    # a follow-up if it becomes annoying.
    served = j.output_url if j.output_url else f"/images/{j.slot_id}.png"
    return ImageJobOut(
        slot_id      = j.slot_id,
        served_url   = served,
        status       = j.status,
        provider_id  = j.provider_id,
        model_id     = j.model_id,
        size         = j.size,
        prompt       = j.prompt,
        output_url   = j.output_url,
        error        = j.error,
        created_at   = j.created_at,
        completed_at = j.completed_at,
        deduplicated = deduplicated,
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/{project_id}/images/request", response_model=ImageBatchResponse)
async def request_images(
    project_id: str,
    body:       ImageBatchRequest,
    user:       User         = Depends(current_user),
    db:         AsyncSession = Depends(get_db),
):
    """Enqueue a batch of image-gen jobs. Returns immediately."""
    project = await _owned_project(project_id, user, db)

    image_model, image_mode, custom_map = await _user_image_config(user, db)

    # Feature must be ON. The agent shouldn't have called this if it isn't,
    # so 409 (Conflict) is the right status — it's a configuration mismatch,
    # not a malformed request.
    if image_mode == "off" or not image_model:
        raise HTTPException(
            409,
            "image generation is disabled for this user — turn it on at Settings → Image AI",
        )

    entry = _resolve_registry_entry(image_model, custom_map)
    if entry is None:
        raise HTTPException(
            409,
            f"saved image model {image_model!r} is not in the registry or custom catalog",
        )

    # Enforce cap. Accept the first N, surface the rest as `rejected` so the
    # agent knows not to embed JSX for them.
    accepted = body.items[:MAX_IMAGES_PER_REQUEST]
    rejected = [
        {"index": i + MAX_IMAGES_PER_REQUEST, "reason": "per_request_cap", "prompt": item.prompt}
        for i, item in enumerate(body.items[MAX_IMAGES_PER_REQUEST:])
    ]

    jobs_out: list[ImageJobOut] = []
    default_size = entry.sizes[0] if entry.sizes else "1024x1024"

    for item in accepted:
        size   = item.size or default_size
        prompt = item.prompt
        dh     = _dedup_hash(entry.provider_id, entry.model_id, size, prompt, item.ref_blob_sha)

        # Dedup check — same hash already in flight or done for this project.
        existing = (await db.execute(
            select(ImageJob).where(
                ImageJob.project_id == project.id,
                ImageJob.dedup_hash == dh,
            )
        )).scalars().first()

        if existing is not None:
            jobs_out.append(_to_out(existing, deduplicated=True))
            continue

        job = ImageJob(
            project_id  = project.id,
            slot_id     = _gen_slot_id(),
            status      = "queued",
            provider_id = entry.provider_id,
            model_id    = entry.model_id,
            prompt      = prompt,
            ref_blob_sha = item.ref_blob_sha,
            size        = size,
            dedup_hash  = dh,
        )
        db.add(job)
        await db.flush()
        await db.refresh(job)
        jobs_out.append(_to_out(job, deduplicated=False))

    await db.commit()
    log.info(
        "imagegen: enqueued %d/%d for project=%s (rejected=%d, dedup=%d)",
        len(jobs_out) - sum(1 for j in jobs_out if j.deduplicated),
        len(jobs_out),
        project.id,
        len(rejected),
        sum(1 for j in jobs_out if j.deduplicated),
    )
    return ImageBatchResponse(jobs=jobs_out, rejected=rejected)


@router.get("/{project_id}/images", response_model=list[ImageJobOut])
async def list_images(
    project_id: str,
    since:      datetime | None = None,
    status:     str | None      = None,
    limit:      int             = 100,
    user:       User            = Depends(current_user),
    db:         AsyncSession    = Depends(get_db),
):
    """Project image-jobs, newest-first. Used by the FE status pill."""
    await _owned_project(project_id, user, db)

    # Cap limit to keep responses bounded. The status pill never needs more
    # than the recent set — agents that need history beyond this can
    # paginate via `since`.
    limit = max(1, min(int(limit or 100), 500))

    q = select(ImageJob).where(ImageJob.project_id == project_id)
    if since is not None:
        q = q.where(ImageJob.created_at > since)
    if status:
        # Strict equality — no partial / fuzzy matches. The known statuses
        # are tiny (queued|running|done|failed); typos should 404-ish, not
        # silently return the wrong slice.
        q = q.where(ImageJob.status == status)
    q = q.order_by(ImageJob.created_at.desc()).limit(limit)

    rows = (await db.execute(q)).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/{project_id}/images/{slot_id}", response_model=ImageJobOut)
async def get_image(
    project_id: str,
    slot_id:    str,
    user:       User         = Depends(current_user),
    db:         AsyncSession = Depends(get_db),
):
    """Single-row lookup. Runner /forge-img resolver hits this."""
    await _owned_project(project_id, user, db)
    job = (await db.execute(
        select(ImageJob).where(
            ImageJob.project_id == project_id,
            ImageJob.slot_id    == slot_id,
        )
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, f"no image job with slot_id={slot_id!r}")
    return _to_out(job)
