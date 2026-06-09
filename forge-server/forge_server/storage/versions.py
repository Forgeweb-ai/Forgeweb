"""
Content-addressed version store.

Replaces the per-snapshot tarball model (see legacy `snapshots.py`) with
per-file deduplication across all projects. A "version" is the project's
state at a point in time, encoded as a manifest `{ rel_path: sha256 }`.
Identical file content is stored exactly once in Supabase Storage.

Lifecycle
─────────
  create_version(db, project, prompt, summary)
    1. Walk workspace, hash each file (excludes node_modules, .git, etc.).
    2. SELECT which shas are already in `blobs`; upload only the new ones.
    3. INSERT rows for newly-uploaded blobs.
    4. INSERT versions row with manifest + parent = project.head_version_id.
    5. Bump blobs.refcount for every DISTINCT sha in the manifest.
    6. Set project.head_version_id = new version.

  restore_version(db, project, target_version_id)            ← soft rollback
    1. Load target manifest.
    2. Walk current workspace; reconcile against target manifest:
         - missing-from-target → delete from disk
         - sha differs / not on disk → fetch blob from Storage, write
    3. Mark descendants of target (between target.exclusive and current head)
       as orphaned_at = now(). Their rows + refcounts stay until GC.
    4. project.head_version_id = target_version_id.

  list_versions(db, project_id, include_orphaned=False)
    Drives the FE dropdown.

Per CLAUDE.md §2: cost shape per turn is O(changed files) for both the
hash-and-diff and the Storage uploads — flat in conversation length, not
linear in project size. Walking the full tree is unavoidable to detect
deletions; we could short-circuit by mtime in v2.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.config import get_settings
from forge_server.db.models import Blob, Project, Version
from forge_server.storage.snapshots import _should_exclude
from forge_server.storage.supabase_storage import SupabaseStorageClient

log = logging.getLogger(__name__)
_settings = get_settings()

# Single bucket holds every blob across every project. Sharding by sha
# prefix happens inside the key, not via separate buckets.
_BLOBS_BUCKET = "forge-blobs"

# Read chunk for streaming sha256. 64KB matches the OS pipe buffer and is
# the sweet spot empirically — smaller chunks waste syscalls, larger
# chunks bloat RAM for no throughput gain.
_HASH_CHUNK = 64 * 1024

# Per-file size ceiling. AI-generated apps shouldn't be checking in 100MB
# binaries; if they do, fail loud rather than silently bloating storage.
# 25MB matches the LFS-ish boundary git uses by convention.
MAX_FILE_BYTES = 25 * 1024 * 1024


def _blob_key(sha: str) -> str:
    """Storage layout: `<first2>/<sha256>`. The 2-char prefix shards the
    bucket 256 ways so no single "directory" accumulates millions of
    objects (a real perf problem in object stores once a prefix exceeds
    ~10k keys)."""
    return f"{sha[:2]}/{sha}"


def _hash_file_sync(path: Path) -> tuple[str, int]:
    """Stream-hash a file. Returns (sha256_hex, size_bytes).

    Raises if the file exceeds MAX_FILE_BYTES — we don't want a single
    huge asset to bloat storage globally, and at scale the bandwidth is
    real money.
    """
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise ValueError(
                    f"File {path} exceeds MAX_FILE_BYTES ({MAX_FILE_BYTES}); "
                    "refusing to snapshot. Move it out of the workspace or "
                    "raise the limit explicitly."
                )
            h.update(chunk)
    return h.hexdigest(), size


def _walk_workspace_sync(workspace_path: str) -> dict[str, tuple[str, int]]:
    """Walk the workspace and return `{rel_path: (sha256, size)}` for every
    snapshottable file. Sync — call via run_in_executor."""
    ws = Path(workspace_path)
    out: dict[str, tuple[str, int]] = {}
    for entry in ws.rglob("*"):
        if not entry.is_file():
            continue
        rel = entry.relative_to(ws).as_posix()
        if _should_exclude(rel):
            continue
        out[rel] = _hash_file_sync(entry)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# create_version
# ─────────────────────────────────────────────────────────────────────────────

async def create_version(
    db: AsyncSession,
    project: Project,
    *,
    prompt:  Optional[str] = None,
    summary: Optional[str] = None,
) -> Version:
    """Capture the current workspace as a new Version.

    Caller owns the AsyncSession; we commit at the end so the row + refcount
    bumps + project.head update are atomic.
    """
    loop = asyncio.get_event_loop()

    # 1. Walk + hash. CPU-bound work off the event loop.
    walked = await loop.run_in_executor(None, _walk_workspace_sync, project.workspace_path)
    if not walked:
        raise RuntimeError(f"Workspace {project.workspace_path} is empty; refusing to create empty version")

    manifest: dict[str, str] = {p: sha for p, (sha, _) in walked.items()}
    distinct_shas = list({sha for sha, _ in walked.values()})

    # 2. Which shas are already known? One round-trip, not N.
    result = await db.execute(select(Blob.sha256).where(Blob.sha256.in_(distinct_shas)))
    known: set[str] = {row[0] for row in result.all()}
    to_upload = [s for s in distinct_shas if s not in known]

    # 3. Upload new blobs, then insert rows. Upload first so a partial
    # failure leaves DB in a clean state (no blobs row pointing at a
    # missing Storage object). We rely on Storage's upsert=true to make
    # double-uploads idempotent across racing snapshots.
    if to_upload:
        # Map sha → (workspace_path, size) so we can re-read content for upload.
        sha_to_meta: dict[str, tuple[Path, int]] = {}
        ws = Path(project.workspace_path)
        for rel, (sha, size) in walked.items():
            if sha in to_upload and sha not in sha_to_meta:
                sha_to_meta[sha] = (ws / rel, size)

        async with SupabaseStorageClient(bucket=_BLOBS_BUCKET) as storage:
            await storage.ensure_bucket(public=False)
            for sha in to_upload:
                path, size = sha_to_meta[sha]
                # Read in the executor — blocking IO.
                data = await loop.run_in_executor(None, path.read_bytes)
                await storage.upload(_blob_key(sha), data, content_type="application/octet-stream")

        # Insert blob rows (refcount starts at 0; the bump-step below
        # increments). ON CONFLICT DO NOTHING handles racing inserts.
        rows = [
            {"sha256": sha, "size_bytes": sha_to_meta[sha][1], "content_type": "application/octet-stream"}
            for sha in to_upload
        ]
        stmt = pg_insert(Blob).values(rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=["sha256"])
        await db.execute(stmt)

    # 3b. No-op short-circuit: if the new manifest is byte-for-byte equal to
    # the current head's manifest, the workspace hasn't changed since the
    # last version. Inserting another row would pollute the dropdown with
    # duplicate entries and waste a refcount bump cycle. This is the
    # property that makes the per-verify-call hook safe — a debug loop
    # that calls verify N times in one AI turn produces at most ONE
    # version (the first one that captured the change).
    #
    # Note: we compare full manifests, not their hash. At ~50-500KB jsonb
    # the equality check is fast and avoids a hash-of-hash dance whose
    # determinism we'd have to defend separately.
    if project.head_version_id:
        parent = await db.get(Version, project.head_version_id)
        if parent is not None and parent.manifest == manifest:
            log.info(
                "Version no-op for project %s: manifest matches head %s (%d files)",
                project.id, parent.id, len(manifest),
            )
            # Still need to commit if we inserted blob rows above. (We
            # didn't bump refcounts yet, so no-op there.)
            await db.commit()
            await db.refresh(parent)
            return parent

    # 4. Insert the version row. Parent = current head (may be None for first version).
    version = Version(
        project_id        = project.id,
        parent_version_id = project.head_version_id,
        prompt            = prompt,
        summary           = summary,
        manifest          = manifest,
    )
    db.add(version)
    await db.flush()  # populate version.id

    # 5. Bump refcounts for every distinct sha referenced by this manifest.
    # One UPDATE for the whole set; cost is O(distinct_shas) not O(files).
    if distinct_shas:
        await db.execute(
            update(Blob)
            .where(Blob.sha256.in_(distinct_shas))
            .values(refcount=Blob.refcount + 1)
        )

    # 6. Move head.
    project.head_version_id = version.id
    project.last_snapshot_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(version)
    log.info(
        "Version %s created: project=%s files=%d new_blobs=%d",
        version.id, project.id, len(manifest), len(to_upload),
    )
    return version


# ─────────────────────────────────────────────────────────────────────────────
# restore_version (soft rollback)
# ─────────────────────────────────────────────────────────────────────────────

async def restore_version(
    db: AsyncSession,
    project: Project,
    target_version_id: str,
) -> Version:
    """Rewind the workspace to a prior version (SOFT).

    Steps:
      1. Load target. Reject if it's not in this project's chain or already orphaned.
      2. Reconcile disk against target.manifest (delete extras, write missing/changed).
      3. Mark every live version with created_at > target.created_at as orphaned.
         (Linear chain assumption — for branching, walk ancestry instead.)
      4. project.head_version_id = target_version_id.

    Refcounts are NOT decremented at orphan time — they stick until the GC
    worker hard-deletes orphaned versions past the grace window. This keeps
    rollback cheap (no big UPDATE) and reversible (user can un-orphan
    within the grace window via "restore newer version").
    """
    target = await db.get(Version, target_version_id)
    if target is None or str(target.project_id) != str(project.id):
        raise ValueError(f"Version {target_version_id} not found for project {project.id}")
    if target.orphaned_at is not None:
        raise ValueError(f"Version {target_version_id} is orphaned; restore it via the GC-recover flow instead")

    loop = asyncio.get_event_loop()
    target_manifest: dict[str, str] = target.manifest or {}

    # Walk current disk to see what's there now (only paths + shas — we
    # need the shas to skip writing files that already match target).
    current = await loop.run_in_executor(None, _walk_workspace_sync, project.workspace_path)
    current_paths = set(current.keys())
    target_paths  = set(target_manifest.keys())

    to_delete = current_paths - target_paths
    to_write  = [(p, target_manifest[p]) for p in target_paths
                 if p not in current or current[p][0] != target_manifest[p]]

    # 2a. Delete files that aren't in target.
    ws = Path(project.workspace_path)
    def _delete_sync() -> None:
        for rel in to_delete:
            try:
                (ws / rel).unlink()
            except FileNotFoundError:
                pass
        # Best-effort: prune empty dirs left behind. Safe because we know
        # SNAPSHOT_EXCLUDES dirs aren't walked here.
        for dirpath, dirnames, filenames in os.walk(ws, topdown=False):
            if not dirnames and not filenames and Path(dirpath) != ws:
                try:
                    Path(dirpath).rmdir()
                except OSError:
                    pass
    await loop.run_in_executor(None, _delete_sync)

    # 2b. Write files that differ. Fetch each missing blob from Storage.
    # Could be parallelised, but at typical churn (1-5 changed files per
    # rollback hop) the sequential path is fine and simpler to reason about.
    if to_write:
        async with SupabaseStorageClient(bucket=_BLOBS_BUCKET) as storage:
            for rel, sha in to_write:
                data = await storage.download(_blob_key(sha))
                full = ws / rel
                full.parent.mkdir(parents=True, exist_ok=True)
                await loop.run_in_executor(None, full.write_bytes, data)

    # 3. Soft-orphan descendants. Linear-chain shortcut: any live version
    # created after the target belongs to the abandoned branch.
    now = datetime.now(timezone.utc)
    await db.execute(
        update(Version)
        .where(
            Version.project_id == project.id,
            Version.created_at > target.created_at,
            Version.orphaned_at.is_(None),
            Version.id != target.id,
        )
        .values(orphaned_at=now)
    )

    # 4. Move head.
    project.head_version_id = target.id

    await db.commit()
    await db.refresh(target)
    log.info(
        "Restored project %s to version %s: deleted=%d wrote=%d",
        project.id, target.id, len(to_delete), len(to_write),
    )
    return target


# ─────────────────────────────────────────────────────────────────────────────
# list_versions
# ─────────────────────────────────────────────────────────────────────────────

async def list_versions(
    db: AsyncSession,
    project_id: str,
    *,
    include_orphaned: bool = False,
    limit: int = 100,
) -> list[Version]:
    """Return versions newest-first. Drives the FE dropdown.

    `limit` is a hard cap — at v1 we don't paginate from the dropdown
    (most projects will have <50 versions; even prolific use rarely
    exceeds 200). When that assumption breaks we'll add a cursor.
    """
    stmt = select(Version).where(Version.project_id == project_id)
    if not include_orphaned:
        stmt = stmt.where(Version.orphaned_at.is_(None))
    stmt = stmt.order_by(Version.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ─────────────────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    "create_version",
    "restore_version",
    "list_versions",
    "MAX_FILE_BYTES",
]
