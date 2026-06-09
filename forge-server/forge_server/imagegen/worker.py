"""
forge_server.imagegen.worker
==============================
Background async drain loop for `image_jobs`.

Lifecycle:
  - Started by `app.lifespan` next to `sleep_worker` (single asyncio task per
    forge-server process).
  - On each tick, claims up to N queued jobs via `SELECT … FOR UPDATE SKIP
    LOCKED LIMIT N`, sets status='running', then dispatches each to an
    adapter call inside an asyncio.Semaphore-capped worker pool.
  - On success: writes the image to storage, sets `output_url` + status='done'.
  - On adapter failure: sets `error` to a stable category + status='failed'.

Why a polling loop + SKIP LOCKED instead of LISTEN/NOTIFY or a queue
service:
  - We're Postgres-only ([[forge_storage_architecture]]) so SKIP LOCKED is
    free durability. LISTEN/NOTIFY would land sooner but at the cost of
    requiring a notify on every request_images insert, which complicates
    the API path; a 2-second poll is already faster than image-gen latency
    (typically 5–30s).
  - At 100k+ containers we shard forge-server replicas anyway; SKIP LOCKED
    means N replicas drain the same queue safely without a coordinator.

Scale shape: ONE forge-server replica supports as many concurrent users as
its `IMAGEGEN_CONCURRENCY` cap permits (default 4). Per-job memory is just
the in-flight image bytes (<5MB typical). Adapters are imported lazily on
first use, so a process that never sees a Replicate job pays 0 bytes for
the Replicate module.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.db.database import AsyncSessionLocal
from forge_server.db.models import ImageJob, UserProviderKey, Project
from forge_server.imagegen import providers as registry
from forge_server.imagegen import storage
from forge_server.imagegen.adapters import get_adapter
from forge_server.imagegen.types import (
    ERROR_AUTH,
    ERROR_RATE_LIMIT,
    ERROR_UNKNOWN,
    GenerateRequest,
    ImageGenError,
)


# In-flight backoff schedule for transient (`rate_limit`) failures. Each
# retry HOLDS the worker semaphore — intentional, it naturally throttles
# burst rate sent to the provider (4 concurrent × 3s backoff = ~1.3/s,
# safely under Replicate's free-tier burst limit). We do NOT in-flight
# retry other error categories: `auth` / `quota` / `content_policy` are
# persistent and retrying just burns the user's BYOK budget on the same
# failure.
_RATE_LIMIT_BACKOFFS_S = (3.0, 8.0, 20.0)

# Re-queue backoff schedule — applied AFTER the in-flight retries above
# have exhausted on rate_limit. Unlike the in-flight loop, re-queueing
# releases the worker semaphore: the job goes back to status='queued' with
# `next_attempt_at = now() + this delay`, and a future worker tick picks
# it up. This is the difference between "hold a slot for hours during a
# provider 429 storm" (a pool-starvation bug at 100k containers) and "park
# it, free the slot, come back later" (bounded, non-blocking, correct).
#
# Schedule is exponential with a hard ceiling. 9 entries → up to 9 re-queues
# → 10 total attempts when combined with the initial claim, matching
# _MAX_ATTEMPTS below. Worst-case total wait across the whole schedule is
# ~5.5h before a stuck job is permanently failed — long enough to ride out
# typical provider rate-limit windows, short enough that a broken provider
# doesn't keep slot+row indefinitely.
_REQUEUE_BACKOFFS_S = (30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 2400.0, 3600.0, 7200.0)

# Cap on `attempts`. Once a job has been claimed this many times and still
# can't get past rate_limit, we mark it permanently failed. The user can
# re-run the turn from the FE if they want another shot — which is the
# right control plane for "this provider is broken, try a different model".
_MAX_ATTEMPTS = 10

log = logging.getLogger("forge.imagegen.worker")


# Tunables — env-overridable so an operator can adjust without a code deploy.
# Defaults are deliberately conservative (4 concurrent provider calls / poll
# every 2s) so a runaway test doesn't burn through someone's BYOK key.
_CONCURRENCY  = int(os.environ.get("IMAGEGEN_CONCURRENCY",  "4"))
_POLL_SECONDS = float(os.environ.get("IMAGEGEN_POLL_SECONDS", "2.0"))
_BATCH_SIZE   = int(os.environ.get("IMAGEGEN_BATCH_SIZE",   "8"))


# ── DB helpers ───────────────────────────────────────────────────────────────

async def _claim_batch(db: AsyncSession, limit: int) -> list[ImageJob]:
    """Atomically claim up to `limit` queued jobs that are DUE NOW.

    SELECT … FOR UPDATE SKIP LOCKED is the standard pattern for
    multi-worker, single-table queues on Postgres. Each claimed row is
    flipped to status='running' so other workers (and a re-scan within the
    same worker) skip it.

    Due-time filter: `next_attempt_at IS NULL` (initial insert, never
    deferred) OR `next_attempt_at <= now()` (re-queued and the parked
    backoff has elapsed). The partial index `ix_image_jobs_queue_due`
    keeps this lookup O(log n) over queued rows regardless of how many
    done/failed rows have accumulated.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ImageJob)
        .where(
            ImageJob.status == "queued",
            or_(
                ImageJob.next_attempt_at.is_(None),
                ImageJob.next_attempt_at <= now,
            ),
        )
        .order_by(ImageJob.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    jobs = list(result.scalars().all())
    if jobs:
        ids = [j.id for j in jobs]
        await db.execute(
            update(ImageJob)
            .where(ImageJob.id.in_(ids))
            .values(status="running", attempts=ImageJob.attempts + 1)
        )
        await db.commit()
    return jobs


async def _resolve_user_key(db: AsyncSession, job: ImageJob, required_provider: str) -> str:
    """Decrypt the user's key for the registry-required provider.

    Lazy-imports the Fernet helper so the worker module doesn't pay the
    cryptography import unless a job is actually being processed.
    """
    # Join: image_jobs → projects.user_id → user_provider_keys
    result = await db.execute(
        select(Project.user_id).where(Project.id == job.project_id)
    )
    user_id = result.scalar_one_or_none()
    if not user_id:
        raise ImageGenError(ERROR_UNKNOWN, "project missing for job")

    result = await db.execute(
        select(UserProviderKey.key_enc).where(
            UserProviderKey.user_id     == user_id,
            UserProviderKey.provider_id == required_provider,
        )
    )
    key_enc = result.scalar_one_or_none()
    if not key_enc:
        # Stable error: the FE can show "add a <provider> key to use this
        # model" instead of a generic auth failure.
        raise ImageGenError(ERROR_AUTH, f"no {required_provider} key on file")

    from forge_server.api.config_routes import _fernet
    try:
        return _fernet().decrypt(key_enc.encode()).decode()
    except Exception as exc:
        raise ImageGenError(ERROR_AUTH, f"failed to decrypt {required_provider} key") from exc


async def _mark_done(job_id: str, output_url: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(ImageJob)
            .where(ImageJob.id == job_id)
            .values(status="done", output_url=output_url, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _mark_failed(job_id: str, category: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(ImageJob)
            .where(ImageJob.id == job_id)
            .values(status="failed", error=category, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _requeue_for_rate_limit(job_id: str, attempts: int) -> bool:
    """Park a rate-limited job for a future attempt.

    Returns True if the job was re-queued, False if attempts is exhausted
    (caller should fall through to `_mark_failed`).

    Sets status back to 'queued' and `next_attempt_at` to the scheduled
    re-attempt time. CRITICAL: we do NOT hold the worker semaphore across
    this wait — the caller drops the slot, this row sits idle in Postgres
    until a future `_claim_batch` picks it up. That's what makes the
    design pool-safe under sustained provider 429s.

    Backoff index is `attempts - 1` because `attempts` was just incremented
    by `_claim_batch` BEFORE this call (we're inside the per-job worker
    after the inner retry loop exhausted). So a job that just finished its
    1st attempt has attempts=1 and should wait _REQUEUE_BACKOFFS_S[0].
    """
    # attempts was incremented at claim time; check the cap and the index
    # bound (extra safety — if MAX_ATTEMPTS grows past the schedule length
    # we degrade gracefully instead of IndexError).
    if attempts >= _MAX_ATTEMPTS or attempts > len(_REQUEUE_BACKOFFS_S):
        return False
    delay_s = _REQUEUE_BACKOFFS_S[attempts - 1]
    # Jitter ±20% so a burst of N rate-limited jobs doesn't all wake up in
    # the same poll tick and re-stampede the provider.
    delay_s = delay_s * (0.8 + random.random() * 0.4)
    next_at = datetime.now(timezone.utc) + timedelta(seconds=delay_s)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(ImageJob)
            .where(ImageJob.id == job_id)
            .values(status="queued", next_attempt_at=next_at)
        )
        await db.commit()
    log.info(
        "imagegen: job %s rate-limited (attempt %d/%d), re-queued for %.0fs",
        job_id, attempts, _MAX_ATTEMPTS, delay_s,
    )
    return True


# ── Per-job worker ───────────────────────────────────────────────────────────

async def _resolve_entry(db: AsyncSession, job: ImageJob):
    """Find the ImageModel entry for this job — built-in first, then per-user
    custom entries from `user_settings.custom_image_providers`.

    Returns the ImageModel or raises ImageGenError(UNKNOWN, …). Doing the
    fallback to user-custom here (instead of at request_images insertion
    time) means user-edits to the catalog take effect on the NEXT pending
    job without re-queuing.
    """
    entry = registry.lookup(job.provider_id, job.model_id)
    if entry is not None:
        return entry

    # Custom-provider path. Look up the project's user, then their
    # custom_image_providers map. Imported lazily so non-custom jobs don't
    # pay the json/db round-trip.
    from forge_server.db.models import UserSettings, Project
    import json as _json

    result = await db.execute(select(Project.user_id).where(Project.id == job.project_id))
    user_id = result.scalar_one_or_none()
    if not user_id:
        raise ImageGenError(ERROR_UNKNOWN, "project missing for job")

    result = await db.execute(
        select(UserSettings.settings_json).where(UserSettings.user_id == user_id)
    )
    raw = result.scalar_one_or_none()
    if not raw:
        raise ImageGenError(ERROR_UNKNOWN, f"model {job.provider_id}/{job.model_id} not in registry")

    try:
        custom = (_json.loads(raw) or {}).get("custom_image_providers") or {}
    except Exception:
        custom = {}

    key = f"{job.provider_id}/{job.model_id}"
    data = custom.get(key)
    if not data:
        raise ImageGenError(ERROR_UNKNOWN, f"model {job.provider_id}/{job.model_id} not in registry or custom catalog")

    entry = registry.custom_entry_from_dict(data)
    if entry is None:
        raise ImageGenError(ERROR_UNKNOWN, f"custom entry for {key} is malformed")
    return entry


async def _resolve_workspace_path(db: AsyncSession, project_id: str) -> str:
    """Read projects.workspace_path. Required for the storage layer post-
    refactor — images live under <workspace>/public/images now, not
    under a global forge-data tree.
    """
    result = await db.execute(select(Project.workspace_path).where(Project.id == project_id))
    ws = result.scalar_one_or_none()
    if not ws:
        raise ImageGenError(ERROR_UNKNOWN, "project workspace path missing")
    return ws


async def _process_job(job: ImageJob, sem: asyncio.Semaphore) -> None:
    """Run one job end-to-end: resolve key → call adapter → store → mark done.

    Transient `rate_limit` errors retry with exponential backoff. Other
    error categories fail immediately — auth/quota/content_policy don't
    self-heal, so retrying just burns BYOK budget on a known failure.
    """
    async with sem:
        # Resolve once outside the retry loop — these aren't part of what
        # rate-limits, and resolving repeatedly would 4x the DB cost.
        try:
            async with AsyncSessionLocal() as db:
                entry          = await _resolve_entry(db, job)
                api_key        = await _resolve_user_key(db, job, entry.required_key_provider)
                workspace_path = await _resolve_workspace_path(db, job.project_id)
        except ImageGenError as exc:
            log.warning("imagegen: job %s setup failed: %s", job.id, exc)
            await _mark_failed(job.id, exc.category)
            return
        except Exception as exc:
            log.exception("imagegen: job %s setup error: %s", job.id, exc)
            await _mark_failed(job.id, ERROR_UNKNOWN)
            return

        adapter = get_adapter(entry.protocol)
        last_category: str | None = None

        for attempt in range(len(_RATE_LIMIT_BACKOFFS_S) + 1):
            try:
                request = GenerateRequest(
                    model_id        = job.model_id,
                    prompt          = job.prompt,
                    size            = job.size,
                    api_key         = api_key,
                    base_url        = entry.base_url,
                    ref_image_bytes = None,   # ref-image loading from blob store is v1.1
                )
                image = await adapter(request)
                output_url = await storage.store(
                    workspace_path = workspace_path,
                    slot_id        = job.slot_id,
                    image          = image,
                )
                await _mark_done(job.id, output_url)
                if attempt > 0:
                    log.info("imagegen: job %s succeeded on retry %d", job.id, attempt)
                else:
                    log.info("imagegen: job %s done (slot=%s)", job.id, job.slot_id)
                return

            except ImageGenError as exc:
                last_category = exc.category
                if exc.category == ERROR_RATE_LIMIT and attempt < len(_RATE_LIMIT_BACKOFFS_S):
                    # Jitter the in-flight backoff so 4 concurrent retries
                    # don't all wake up and thunder onto the provider in
                    # the same tick.
                    delay = _RATE_LIMIT_BACKOFFS_S[attempt] + random.uniform(0.0, 1.0)
                    log.info(
                        "imagegen: job %s rate-limited (inner-retry %d), waiting %.1fs",
                        job.id, attempt + 1, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                # Inner retries exhausted on rate_limit → re-queue with
                # backoff (releases the semaphore) IF attempts remain.
                # Other error categories (auth/quota/content_policy) fail
                # immediately — they don't self-heal.
                if exc.category == ERROR_RATE_LIMIT:
                    if await _requeue_for_rate_limit(job.id, job.attempts):
                        return  # parked; future tick will drain it
                    # else fall through to permanent fail
                log.warning("imagegen: job %s failed: %s", job.id, exc)
                await _mark_failed(job.id, exc.category)
                return

            except Exception as exc:
                log.exception("imagegen: job %s unexpected error: %s", job.id, exc)
                await _mark_failed(job.id, ERROR_UNKNOWN)
                return

        # Loop exited via the retry-exhausted path (rate_limit on every try
        # within the inner loop, without raising — defensive). Try re-queue,
        # then fall through to fail.
        if last_category == ERROR_RATE_LIMIT and await _requeue_for_rate_limit(job.id, job.attempts):
            return
        await _mark_failed(job.id, last_category or ERROR_UNKNOWN)


# ── Loop ─────────────────────────────────────────────────────────────────────

async def _tick(sem: asyncio.Semaphore) -> None:
    async with AsyncSessionLocal() as db:
        jobs = await _claim_batch(db, _BATCH_SIZE)

    if not jobs:
        return

    log.info("imagegen: claimed %d job(s)", len(jobs))
    # Fan out — semaphore bounds concurrency below the in-flight cap.
    await asyncio.gather(*(_process_job(j, sem) for j in jobs))


async def image_worker() -> None:
    """Long-running task. Mirrors `sleep_worker` lifecycle for app.lifespan."""
    sem = asyncio.Semaphore(_CONCURRENCY)
    log.info(
        "Image worker started — concurrency=%d poll=%.1fs batch=%d",
        _CONCURRENCY, _POLL_SECONDS, _BATCH_SIZE,
    )
    while True:
        try:
            await _tick(sem)
            await asyncio.sleep(_POLL_SECONDS)
        except asyncio.CancelledError:
            log.info("Image worker cancelled — shutting down")
            break
        except Exception as exc:
            log.error("Image worker loop error: %s", exc, exc_info=True)
            await asyncio.sleep(_POLL_SECONDS)
