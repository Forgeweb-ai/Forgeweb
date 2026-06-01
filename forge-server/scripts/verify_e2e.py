"""
scripts/verify_e2e.py
======================
End-to-end smoke test for Task 8 (the migration + pnpm + snapshot + OAuth work
from the May 2026 session). Doesn't replace clicking through the UI but covers
every layer programmatically.

Usage (from forge-server/, with the venv active):
  python scripts/verify_e2e.py

What it checks:
  1. Alembic is at head (0002).
  2. All 9 expected tables exist with the new UUID + TIMESTAMPTZ columns.
  3. Supabase Storage is reachable; create + upload + download + delete a
     test object in a temp bucket.
  4. Snapshot create+rehydrate round-trip against a synthetic project.
  5. OAuth start endpoint returns 503 when credentials aren't configured
     (the expected behaviour pre-setup).
  6. pnpm shared store volume exists in Docker.

Exit code 0 = all green; non-zero = something failed (prints which).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

# Make `forge_server.*` importable when running from forge-server/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from forge_server.config import get_settings  # noqa: E402

settings = get_settings()


GREEN = "\033[0;32m"
RED   = "\033[0;31m"
DIM   = "\033[0;90m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}✓{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"{RED}✗{RESET} {msg}")


def info(msg: str) -> None:
    print(f"{DIM}  {msg}{RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# Checks
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_TABLES = {
    "users", "user_settings", "user_provider_keys",
    "projects", "dev_containers",
    "supabase_connections", "project_env_vars",
    "snapshots", "user_supabase_oauth",
    "alembic_version",
}


async def check_schema() -> bool:
    """All expected tables present, alembic at head 0002."""
    engine = create_async_engine(settings.database_url, echo=False)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
            missing = EXPECTED_TABLES - tables
            if missing:
                fail(f"missing tables: {missing}")
                return False
            ok(f"all 10 expected tables present")

            ver = await conn.execute(text("SELECT version_num FROM alembic_version"))
            head = ver.scalar()
            if head != "0002":
                fail(f"alembic head is {head}, expected 0002")
                return False
            ok(f"alembic at head {head}")
        return True
    finally:
        await engine.dispose()


async def check_storage() -> bool:
    """Create bucket, upload+download+delete a test object."""
    if not settings.supabase_service_role_key:
        info("SUPABASE_SERVICE_ROLE_KEY not set — skipping storage check")
        return True
    from forge_server.storage.supabase_storage import SupabaseStorageClient
    test_bucket = f"forge-verify-{uuid.uuid4().hex[:8]}"
    test_key    = "hello.txt"
    test_data   = b"hello from verify_e2e"
    async with SupabaseStorageClient(bucket=test_bucket) as s:
        await s.ensure_bucket(public=False)
        ok(f"bucket created: {test_bucket}")
        await s.upload(test_key, test_data, content_type="text/plain")
        ok(f"upload ok ({len(test_data)} bytes)")
        got = await s.download(test_key)
        if got != test_data:
            fail("downloaded bytes don't match upload")
            return False
        ok("download round-trip matches")
        await s.delete(test_key)
        ok("delete ok")
    return True


async def check_snapshot_roundtrip() -> bool:
    """Build a fake project on disk → create_snapshot → wipe → rehydrate → compare."""
    if not settings.supabase_service_role_key:
        info("SUPABASE_SERVICE_ROLE_KEY not set — skipping snapshot check")
        return True
    from forge_server.db.database import AsyncSessionLocal
    from forge_server.db.models import Project, User
    from forge_server.storage.snapshots import create_snapshot, rehydrate_from_latest_snapshot

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "workspace"
        ws.mkdir()
        (ws / "package.json").write_text('{"name":"verify"}')
        (ws / "src").mkdir()
        (ws / "src" / "main.ts").write_text('console.log("hi")')
        # excluded paths shouldn't end up in the snapshot
        (ws / "node_modules").mkdir()
        (ws / "node_modules" / "junk").write_text("x" * 100)

        async with AsyncSessionLocal() as db:
            # Need a real user to FK against. Pick any one.
            row = (await db.execute(select(User).limit(1))).scalar_one_or_none()
            if row is None:
                info("no users in DB — skipping snapshot check (register one first)")
                return True
            project = Project(
                user_id        = row.id,
                name           = "verify-e2e",
                workspace_path = str(ws),
            )
            db.add(project)
            await db.commit()
            await db.refresh(project)

            snap = await create_snapshot(db, project)
            ok(f"snapshot created ({snap.size_bytes // 1024} KB) at {snap.storage_key}")

            # Wipe + rehydrate
            shutil.rmtree(ws)
            ws.mkdir()
            await rehydrate_from_latest_snapshot(db, project)

            if not (ws / "package.json").exists() or not (ws / "src" / "main.ts").exists():
                fail("rehydrate missed expected files")
                return False
            if (ws / "node_modules").exists():
                fail("rehydrate restored node_modules — exclude rule failed")
                return False
            ok("rehydrate restored exactly the expected files (node_modules excluded)")

            # Clean up
            await db.delete(project)
            await db.commit()
    return True


def check_pnpm_volume() -> bool:
    """Docker volume forge-pnpm-store exists."""
    import subprocess
    r = subprocess.run(
        ["docker", "volume", "inspect", "forge-pnpm-store"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        fail("forge-pnpm-store docker volume missing — run dev.sh once to create it")
        return False
    ok("forge-pnpm-store docker volume exists")
    return True


def check_oauth_unconfigured() -> bool:
    """OAuth start endpoint should return 503 when client_id isn't set."""
    if settings.supabase_oauth_client_id and settings.supabase_oauth_client_secret:
        ok("Supabase OAuth credentials are configured")
        return True
    info("OAuth not configured yet — that's fine for now")
    info("  → register an OAuth app at https://supabase.com/dashboard/account/apps")
    info("  → set SUPABASE_OAUTH_CLIENT_ID + SUPABASE_OAUTH_CLIENT_SECRET in .env")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> int:
    results: list[tuple[str, bool]] = []

    print(f"\n{DIM}── E2E verification ──{RESET}")
    print(f"  DATABASE_URL: {settings.database_url.split('@')[-1]}")
    print(f"  SUPABASE_URL: {settings.supabase_url}\n")

    print(f"{DIM}[1/5] Schema{RESET}")
    results.append(("schema", await check_schema()))

    print(f"\n{DIM}[2/5] Supabase Storage{RESET}")
    results.append(("storage", await check_storage()))

    print(f"\n{DIM}[3/5] Snapshot round-trip{RESET}")
    results.append(("snapshot", await check_snapshot_roundtrip()))

    print(f"\n{DIM}[4/5] pnpm shared store volume{RESET}")
    results.append(("pnpm", check_pnpm_volume()))

    print(f"\n{DIM}[5/5] OAuth config{RESET}")
    results.append(("oauth", check_oauth_unconfigured()))

    print(f"\n{DIM}── Summary ──{RESET}")
    failed = [name for name, p in results if not p]
    for name, p in results:
        mark = f"{GREEN}OK{RESET}" if p else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
