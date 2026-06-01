"""
Workspace snapshot + rehydrate, persisted to Supabase Storage.

A snapshot is a single .tar.zst (or .tar.gz fallback) blob containing the
project's workspace files, excluding regenerable noise (node_modules, .next,
dist, build artifacts). The on-disk workspace stays as the hot copy; snapshots
are the durability + portability layer. See [[forge_storage_architecture]].
"""
from __future__ import annotations

import asyncio
import io
import logging
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.config import get_settings
from forge_server.db.models import Project, Snapshot
from forge_server.storage.supabase_storage import SupabaseStorageClient

log = logging.getLogger(__name__)

_settings = get_settings()

# Anything matching these prefixes/suffixes is excluded from snapshots. The
# pnpm shared store handles node_modules dedup; .next/dist are regenerable.
SNAPSHOT_EXCLUDES: tuple[str, ...] = (
    "node_modules",
    ".next",
    "dist",
    "build",
    ".turbo",
    ".cache",
    ".vite",
    "__pycache__",
    ".git",
)


def _should_exclude(rel_path: str) -> bool:
    """True if rel_path is inside any excluded directory."""
    parts = Path(rel_path).parts
    return any(p in SNAPSHOT_EXCLUDES for p in parts)


def _build_tarball_sync(workspace_path: str) -> bytes:
    """Synchronous tar.gz builder. Called via run_in_executor to avoid blocking.

    We use gzip rather than zstd to stay deps-free (tarfile gzip is stdlib).
    Compression is ~30% worse than zstd but fine for our scale, and avoids
    pulling in zstandard + libzstd-dev on every platform.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        ws = Path(workspace_path)
        for entry in ws.rglob("*"):
            if not entry.is_file():
                continue
            rel = entry.relative_to(ws).as_posix()
            if _should_exclude(rel):
                continue
            tar.add(entry, arcname=rel, recursive=False)
    return buf.getvalue()


def _extract_tarball_sync(data: bytes, dest_path: str) -> None:
    """Synchronous tar extract. Called via run_in_executor."""
    Path(dest_path).mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO(data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        # Defensive: refuse absolute paths or ".." traversal in tar members.
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise RuntimeError(f"Refusing unsafe tar member: {m.name}")
        tar.extractall(dest_path)


def _storage_key(user_id: str, project_id: str, ts: datetime) -> str:
    """Storage layout: {user_id}/{project_id}/{ISO timestamp}.tar.gz"""
    safe_ts = ts.strftime("%Y%m%dT%H%M%SZ")
    return f"{user_id}/{project_id}/{safe_ts}.tar.gz"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def create_snapshot(db: AsyncSession, project: Project) -> Snapshot:
    """Tar+upload the project's workspace, write a Snapshot row, update
    project.last_snapshot_at. Returns the new Snapshot.

    Caller owns the AsyncSession; we commit here so the snapshot row is
    durable before the function returns.
    """
    loop = asyncio.get_event_loop()
    now  = datetime.now(timezone.utc)
    key  = _storage_key(str(project.user_id), str(project.id), now)

    log.info("Snapshotting project %s → %s", project.id, key)
    blob = await loop.run_in_executor(None, _build_tarball_sync, project.workspace_path)

    async with SupabaseStorageClient(bucket=_settings.snapshots_bucket) as storage:
        await storage.ensure_bucket(public=False)
        await storage.upload(key, blob, content_type="application/gzip")

    snap = Snapshot(
        project_id  = project.id,
        storage_key = key,
        size_bytes  = len(blob),
    )
    db.add(snap)
    project.last_snapshot_at = now
    await db.commit()
    await db.refresh(snap)
    log.info("Snapshot %s saved (%d KB)", snap.id, len(blob) // 1024)
    return snap


async def latest_snapshot_for(db: AsyncSession, project_id: str) -> Optional[Snapshot]:
    """Most recent snapshot for the project, or None if none exist."""
    result = await db.execute(
        select(Snapshot)
        .where(Snapshot.project_id == project_id)
        .order_by(Snapshot.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def rehydrate_from_latest_snapshot(
    db: AsyncSession,
    project: Project,
) -> Optional[Snapshot]:
    """If the workspace dir is missing OR forced, download+extract the latest
    snapshot into project.workspace_path. Returns the snapshot used, or None
    if there's nothing to rehydrate from.

    Idempotent — extracting onto an already-populated dir overwrites files
    deterministically. Caller is expected to run `pnpm install` (or equivalent)
    after rehydrate to repopulate node_modules from the shared store.
    """
    snap = await latest_snapshot_for(db, str(project.id))
    if snap is None:
        log.info("Rehydrate skipped: no snapshots exist for project %s", project.id)
        return None

    log.info("Rehydrating project %s from %s", project.id, snap.storage_key)
    async with SupabaseStorageClient(bucket=_settings.snapshots_bucket) as storage:
        blob = await storage.download(snap.storage_key)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _extract_tarball_sync, blob, project.workspace_path)
    log.info("Rehydrate complete: %d KB → %s", len(blob) // 1024, project.workspace_path)
    return snap
