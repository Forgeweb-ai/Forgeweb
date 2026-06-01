"""
scripts/migrate_workspaces_remove_forge_files.py
=================================================
One-shot migration that backfills each existing project's `forge.json` into
the projects table, then deletes the Forge-managed files from every workspace:

  forge.json
  AGENTS.md
  .opencode/

Idempotent: safe to re-run. If a workspace has already been cleaned, the
script reports it as "already clean" and skips the delete.

Usage (run from forge-server/):

    python scripts/migrate_workspaces_remove_forge_files.py             # apply
    python scripts/migrate_workspaces_remove_forge_files.py --dry-run   # preview only

Pre-reqs:
  - alembic migration 0004_project_services_json has been applied (adds
    Project.services_json).
  - DATABASE_URL env var points at the destination Postgres.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Make `forge_server` importable when run from the forge-server/ root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge_server.db.models import Project  # noqa: E402


FORGE_FILES = ("forge.json", "AGENTS.md", ".opencode")


def _scan_workspaces(forge_data_root: Path) -> list[Path]:
    """Find every existing workspace directory under forge-data/users/."""
    base = forge_data_root / "users"
    if not base.exists():
        return []
    return sorted(base.glob("*/projects/*/workspace"))


def _parse_project_id(workspace: Path) -> str | None:
    """Extract project_id from forge-data/users/<u>/projects/<p>/workspace path."""
    parts = workspace.parts
    try:
        i = parts.index("projects")
    except ValueError:
        return None
    if i + 1 >= len(parts):
        return None
    return parts[i + 1]


def _read_forge_json(workspace: Path) -> dict[str, Any] | None:
    p = workspace / "forge.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def _backfill_fields_from_forge_json(project: Project, forge: dict[str, Any]) -> list[str]:
    """Apply forge.json fields onto the Project row. Returns list of fields changed."""
    changed: list[str] = []

    # stack
    if forge.get("stack") and not project.stack:
        project.stack = forge["stack"]
        changed.append("stack")

    # services → services_json (only if not already set)
    services = forge.get("services")
    if services and not project.services_json:
        project.services_json = json.dumps(services)
        changed.append("services_json")

    # description (preserve DB if present)
    if forge.get("description") and not project.description:
        project.description = forge["description"]
        changed.append("description")

    return changed


def _delete_forge_files(workspace: Path) -> list[str]:
    """Remove forge.json, AGENTS.md, .opencode/. Returns list of paths deleted."""
    deleted: list[str] = []
    for name in FORGE_FILES:
        target = workspace / name
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
        deleted.append(name)
    return deleted


async def _run(database_url: str, forge_data_root: Path, dry_run: bool) -> None:
    engine = create_async_engine(database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    workspaces = _scan_workspaces(forge_data_root)
    print(f"Found {len(workspaces)} workspace(s) under {forge_data_root}")
    print()

    stats = {
        "total":       len(workspaces),
        "no_project":  0,
        "backfilled":  0,
        "cleaned":     0,
        "already":     0,
    }

    async with Session() as db:
        for ws in workspaces:
            project_id = _parse_project_id(ws)
            if not project_id:
                print(f"  [skip] could not parse project_id from {ws}")
                stats["no_project"] += 1
                continue

            row = (
                await db.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()

            if row is None:
                print(f"  [skip] {project_id}: no DB row for this workspace")
                stats["no_project"] += 1
                continue

            # 1. Backfill from forge.json into the DB
            forge = _read_forge_json(ws)
            if forge:
                changed = _backfill_fields_from_forge_json(row, forge)
                if changed:
                    stats["backfilled"] += 1
                    print(f"  [backfill] {project_id}: {', '.join(changed)}")
            elif (ws / "forge.json").exists():
                # File exists but couldn't be parsed
                print(f"  [warn] {project_id}: forge.json present but unparseable")

            # 2. Delete the Forge-managed files
            already_clean = not any((ws / name).exists() for name in FORGE_FILES)
            if already_clean:
                stats["already"] += 1
                continue

            if dry_run:
                preview = [name for name in FORGE_FILES if (ws / name).exists()]
                print(f"  [dry-run] would delete from {project_id}: {', '.join(preview)}")
            else:
                deleted = _delete_forge_files(ws)
                if deleted:
                    print(f"  [clean] {project_id}: deleted {', '.join(deleted)}")
                    stats["cleaned"] += 1

        if not dry_run:
            await db.commit()

    await engine.dispose()

    print()
    print("=== Summary ===")
    for k, v in stats.items():
        print(f"  {k:12s} {v}")
    if dry_run:
        print("  (dry-run — no changes applied)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--forge-data-root",
        default=os.environ.get("FORGE_DATA_ROOT", "../forge-data"),
        help="Root directory containing users/{u}/projects/{p}/workspace (default: $FORGE_DATA_ROOT or ../forge-data)",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://forge:forgedev@localhost:5432/forge",
        ),
        help="SQLAlchemy URL for the Postgres DB (default: $DATABASE_URL)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing anything")
    args = parser.parse_args()

    forge_data_root = Path(args.forge_data_root).resolve()
    if not forge_data_root.exists():
        sys.exit(f"forge-data root not found: {forge_data_root}")

    asyncio.run(_run(args.database_url, forge_data_root, args.dry_run))


if __name__ == "__main__":
    main()
