"""
Background snapshot worker — periodically scans for projects whose workspace
has been modified since their last snapshot, and triggers create_snapshot
for them. Runs forever as an asyncio task; started from app.py lifespan.

Heuristic for "needs snapshot":
  projects.updated_at > projects.last_snapshot_at
  OR
  projects.last_snapshot_at IS NULL AND projects.created_at < NOW() - min_age

Bounded — at most ~5 snapshots per tick to avoid one big project saturating
egress.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from forge_server.config import get_settings
from forge_server.db.database import AsyncSessionLocal
from forge_server.db.models import Project
from forge_server.storage.snapshots import create_snapshot

log = logging.getLogger(__name__)

_settings = get_settings()

MAX_SNAPSHOTS_PER_TICK = 5


async def _dirty_projects(limit: int) -> list[Project]:
    """Projects that have been modified since their last snapshot."""
    min_age_cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=_settings.snapshot_min_age
    )
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Project)
            .where(
                or_(
                    # changed since last snapshot
                    and_(
                        Project.last_snapshot_at.is_not(None),
                        Project.updated_at > Project.last_snapshot_at,
                    ),
                    # never snapshotted, and old enough to be worth capturing
                    and_(
                        Project.last_snapshot_at.is_(None),
                        Project.created_at < min_age_cutoff,
                    ),
                )
            )
            .order_by(Project.updated_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def snapshot_tick() -> None:
    """One scan + snapshot pass. Safe to call from anywhere."""
    if not _settings.supabase_service_role_key:
        log.debug("snapshot_tick: SUPABASE_SERVICE_ROLE_KEY not set, skipping")
        return
    projects = await _dirty_projects(MAX_SNAPSHOTS_PER_TICK)
    if not projects:
        return
    log.info("snapshot_tick: %d project(s) need snapshotting", len(projects))
    for project in projects:
        try:
            async with AsyncSessionLocal() as db:
                # Re-fetch under the new session so SQLAlchemy state is clean.
                refetch = await db.get(Project, project.id)
                if refetch is None:
                    continue
                await create_snapshot(db, refetch)
        except Exception:
            log.exception("snapshot failed for project %s", project.id)


async def snapshot_worker() -> None:
    """Long-running asyncio task. Cancellation-aware."""
    log.info(
        "snapshot worker started (interval=%ds, min_age=%ds)",
        _settings.snapshot_check_interval, _settings.snapshot_min_age,
    )
    while True:
        try:
            await snapshot_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("snapshot_tick raised — continuing")
        await asyncio.sleep(_settings.snapshot_check_interval)
