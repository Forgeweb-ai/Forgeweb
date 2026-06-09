"""
forge_server/runner/sleep_manager.py
======================================
Background asyncio task that auto-stops idle project containers.

Logic:
  Every `sleep_check_interval` seconds (default 60s):
    - Query DevContainer rows with status='running'
    - If last_ping_at is older than `idle_ttl` seconds (default 600s = 10min)
      → docker stop the container
      → update DB status to 'sleeping'

forge-ui sends a ping to /api/dev/ping every 2 minutes while the
preview iframe is visible. When the user closes their session or tab,
pings stop, and after 10 min the container sleeps automatically.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update

from forge_server.config import get_settings
from forge_server.db.database import AsyncSessionLocal
from forge_server.db.models import DevContainer
from forge_server.runner.container_manager import container_manager

log = logging.getLogger("forge.sleep_manager")
settings = get_settings()


async def _sleep_idle_containers() -> None:
    """Single pass: find idle containers and stop them."""
    cutoff = datetime.utcnow() - timedelta(seconds=settings.idle_ttl)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DevContainer).where(
                DevContainer.status == "running",
                DevContainer.last_ping_at < cutoff,
            )
        )
        idle = result.scalars().all()

    for dc in idle:
        try:
            log.info(
                "Sleeping idle container project_id=%s last_ping=%s",
                dc.project_id,
                dc.last_ping_at,
            )
            await container_manager.stop(dc.project_id)

            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(DevContainer)
                    .where(DevContainer.project_id == dc.project_id)
                    .values(status="sleeping", updated_at=datetime.utcnow())
                )
                await db.commit()

        except Exception as exc:
            log.warning("Failed to sleep container project_id=%s: %s", dc.project_id, exc)


async def sleep_worker() -> None:
    """
    Long-running background task. Start this in app lifespan.

    asyncio.CancelledError is expected on shutdown — handle gracefully.
    """
    log.info(
        "Sleep worker started — check_interval=%ds idle_ttl=%ds",
        settings.sleep_check_interval,
        settings.idle_ttl,
    )
    while True:
        try:
            await asyncio.sleep(settings.sleep_check_interval)
            await _sleep_idle_containers()
        except asyncio.CancelledError:
            log.info("Sleep worker cancelled — shutting down")
            break
        except Exception as exc:
            # Never crash the worker — log and continue
            log.error("Sleep worker error: %s", exc, exc_info=True)
