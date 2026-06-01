#!/usr/bin/env python3
"""
repair_workspaces.py
====================
One-shot structural repair for existing Forge project workspaces.

Run after deploying the layout-aware bootstrap. For every workspace under
$FORGE_DATA_ROOT/users/*/projects/*/workspace, this script:

1. Detects the actual app layout (no-src vs src/).

2. If a bootstrap-generated `src/app/api/items/` or `src/lib/db/` tree exists
   in a NO-SRC project (the bug your existing 10 projects hit), MOVES the
   drizzle scaffold into the app's real tree and rewrites drizzle.config.ts
   so `npx drizzle-kit migrate` reads the right schema.

3. If a workspace contains BOTH `lib/db/client.ts` (drizzle scaffold) and a
   raw `lib/db.ts` (agent freelanced with better-sqlite3), writes a
   `REPAIR.md` flag at the workspace root. The build agent's next prompt
   will see it and run a one-turn cleanup. We do NOT auto-rewrite the agent's
   custom code — too risky.

4. Adds `display: "swap"` to every `next/font/google` call in `app/layout.tsx`
   or `src/app/layout.tsx` that doesn't already have it. This fixes the
   "Loading…" + nothing clickable hydration symptom.

Usage:
    # Dry run (default — prints what it would change):
    python3 -m forge_server.scripts.repair_workspaces

    # Actually apply:
    python3 -m forge_server.scripts.repair_workspaces --apply

    # Single project:
    python3 -m forge_server.scripts.repair_workspaces --project-id <uuid> --apply

Exit codes:
    0 — finished, no errors
    1 — at least one workspace failed to repair (others may have succeeded)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ── Detection helpers ────────────────────────────────────────────────────────

def detect_layout(ws: Path) -> str:
    """Returns 'no_src' | 'src' | 'unknown'."""
    if (ws / "app").is_dir() or (ws / "pages").is_dir():
        return "no_src"
    if (ws / "src" / "app").is_dir() or (ws / "src" / "pages").is_dir():
        return "src"
    return "unknown"


def has_dead_src_scaffold(ws: Path, layout: str) -> bool:
    """True if scaffold landed in src/ but the app is no-src — needs moving."""
    if layout != "no_src":
        return False
    return (
        (ws / "src" / "lib" / "db" / "client.ts").exists()
        or (ws / "src" / "app" / "api" / "items").exists()
    )


def has_raw_better_sqlite(ws: Path) -> bool:
    """
    True if the agent freelanced a raw better-sqlite3 client outside the
    drizzle scaffold. We check for `import Database from "better-sqlite3"`
    in any file that ISN'T the canonical client.ts.
    """
    pattern = re.compile(r'''import\s+Database\s+from\s+["']better-sqlite3["']''')
    for p in ws.rglob("*.ts"):
        if "node_modules" in p.parts:
            continue
        if p.name == "client.ts" and "db" in p.parent.name:
            continue
        try:
            if pattern.search(p.read_text(errors="ignore")):
                return True
        except Exception:
            continue
    return False


# ── Repair actions ───────────────────────────────────────────────────────────

@dataclass
class RepairPlan:
    workspace:           Path
    layout:              str
    move_scaffold:       bool = False
    rewrite_drizzle_cfg: bool = False
    fix_font_display:    list[Path] = field(default_factory=list)
    flag_raw_sqlite:     bool = False

    def is_empty(self) -> bool:
        return not (
            self.move_scaffold
            or self.rewrite_drizzle_cfg
            or self.fix_font_display
            or self.flag_raw_sqlite
        )


def plan_repair(ws: Path) -> RepairPlan:
    layout = detect_layout(ws)
    plan = RepairPlan(workspace=ws, layout=layout)

    # 1. Dead scaffold in src/ for a no-src project
    if has_dead_src_scaffold(ws, layout):
        plan.move_scaffold = True
        plan.rewrite_drizzle_cfg = True

    # 2. Font display:swap fix
    layout_paths = [
        ws / "app" / "layout.tsx",
        ws / "src" / "app" / "layout.tsx",
    ]
    font_pattern = re.compile(
        r"""(Inter|Roboto|Playfair_Display|JetBrains_Mono|Geist|Geist_Mono|Roboto_Mono|Source_Sans_3|Lora|Merriweather|Open_Sans|Poppins|Montserrat|Nunito|Raleway|Work_Sans|Fira_Code|Space_Grotesk)\s*\(\s*\{([^}]*?)\}\s*\)""",
        re.DOTALL,
    )
    for p in layout_paths:
        if not p.exists():
            continue
        text = p.read_text()
        needs_fix = False
        for m in font_pattern.finditer(text):
            body = m.group(2)
            if "display" not in body:
                needs_fix = True
                break
        if needs_fix:
            plan.fix_font_display.append(p)

    # 3. Raw better-sqlite3 outside client.ts
    if has_raw_better_sqlite(ws):
        plan.flag_raw_sqlite = True

    return plan


def apply_move_scaffold(ws: Path) -> list[str]:
    """Move src/lib/db and src/app/api/items into the no-src tree."""
    actions: list[str] = []

    pairs = [
        (ws / "src" / "lib" / "db",            ws / "lib" / "db"),
        (ws / "src" / "app" / "api" / "items", ws / "app" / "api" / "items"),
    ]
    for src, dst in pairs:
        if not src.exists():
            continue
        if dst.exists():
            actions.append(f"skip move: {dst.relative_to(ws)} already exists")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        actions.append(f"moved {src.relative_to(ws)} → {dst.relative_to(ws)}")

    # Clean up empty parent dirs
    for d in [ws / "src" / "lib", ws / "src" / "app" / "api", ws / "src" / "app", ws / "src"]:
        try:
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
                actions.append(f"removed empty {d.relative_to(ws)}")
        except OSError:
            pass

    return actions


def apply_drizzle_cfg_rewrite(ws: Path) -> list[str]:
    cfg = ws / "drizzle.config.ts"
    if not cfg.exists():
        return []
    text = cfg.read_text()
    new = text.replace('"./src/lib/db/schema.ts"', '"./lib/db/schema.ts"')
    if new != text:
        cfg.write_text(new)
        return ["rewrote drizzle.config.ts schema path → ./lib/db/schema.ts"]
    return []


def apply_font_display_fix(p: Path) -> list[str]:
    """
    Insert `display: "swap"` into every next/font/google call in this file
    that doesn't already have a `display:` key. Conservative — only touches
    call sites we can match.
    """
    text = p.read_text()
    # Match: SomeFont({ ... })
    pattern = re.compile(
        r"""(Inter|Roboto|Playfair_Display|JetBrains_Mono|Geist|Geist_Mono|Roboto_Mono|Source_Sans_3|Lora|Merriweather|Open_Sans|Poppins|Montserrat|Nunito|Raleway|Work_Sans|Fira_Code|Space_Grotesk)\s*\(\s*\{([^}]*?)\}\s*\)""",
        re.DOTALL,
    )

    def repl(m: re.Match[str]) -> str:
        name, body = m.group(1), m.group(2)
        if "display" in body:
            return m.group(0)
        # Insert before the closing brace, preserving indentation of last line
        body_stripped = body.rstrip()
        sep = "," if body_stripped and not body_stripped.endswith(",") else ""
        return f'{name}({{{body}{sep}\n  display: "swap",\n}})'

    new = pattern.sub(repl, text)
    if new != text:
        p.write_text(new)
        return [f'added display:"swap" to next/font calls in {p.relative_to(p.parents[len(p.parents)-1])}']
    return []


def apply_raw_sqlite_flag(ws: Path) -> list[str]:
    """Drop a REPAIR.md the agent will see on next session open."""
    flag = ws / "REPAIR.md"
    if flag.exists():
        return ["REPAIR.md already present"]
    flag.write_text(
        "# Forge — structural repair pending\n\n"
        "This project has a Drizzle scaffold at `lib/db/client.ts` AND a raw\n"
        "`better-sqlite3` client elsewhere (likely `lib/db.ts`). On the next\n"
        "user prompt, run this one-time cleanup BEFORE addressing the prompt:\n\n"
        "1. Read the raw client (likely `lib/db.ts` or similar) and identify\n"
        "   every `CREATE TABLE` it issues plus every helper function it exports.\n"
        "2. Port each table into `lib/db/schema.ts` as a `sqliteTable(...)` —\n"
        "   one table per export, same column types, same NOT NULL / DEFAULT.\n"
        "3. Rewrite every importer of the raw client to import from\n"
        "   `@/lib/db/client` and `@/lib/db/schema` instead, using Drizzle\n"
        "   query builders (`db.select().from(X)`, `db.insert(X).values(...)`)\n"
        "   instead of `db.prepare(...).all()` / `.run()`.\n"
        "4. Run `npx drizzle-kit generate && npx drizzle-kit migrate` so the\n"
        "   canonical `data.db` matches the new schema.\n"
        "5. Delete the raw client file. Delete this REPAIR.md.\n\n"
        "Do not skip steps. The Data tab and 'Migrate to Supabase' both\n"
        "depend on the canonical scaffold being the single source of truth.\n"
    )
    return ["wrote REPAIR.md flag — agent will run cleanup on next session"]


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply changes (default: dry run)")
    parser.add_argument("--project-id", default=None, help="repair only this project UUID")
    parser.add_argument(
        "--root",
        default=os.environ.get("FORGE_DATA_ROOT", "/forge-data"),
        help="forge data root (default: $FORGE_DATA_ROOT or /forge-data)",
    )
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"FORGE_DATA_ROOT {root} not found", file=sys.stderr)
        return 1

    users_root = root / "users"
    if not users_root.exists():
        print(f"No users/ under {root}", file=sys.stderr)
        return 1

    targets: list[Path] = []
    for user_dir in users_root.iterdir():
        if not user_dir.is_dir():
            continue
        projects_dir = user_dir / "projects"
        if not projects_dir.is_dir():
            continue
        for proj_dir in projects_dir.iterdir():
            if args.project_id and proj_dir.name != args.project_id:
                continue
            ws = proj_dir / "workspace"
            if ws.is_dir():
                targets.append(ws)

    if not targets:
        print("No workspaces found.")
        return 0

    failures = 0
    summary = {"workspaces": []}

    for ws in targets:
        plan = plan_repair(ws)
        rel = ws.relative_to(root)
        if plan.is_empty():
            summary["workspaces"].append({"workspace": str(rel), "actions": ["nothing to do"]})
            continue

        actions: list[str] = []
        try:
            if args.apply:
                if plan.move_scaffold:
                    actions.extend(apply_move_scaffold(ws))
                if plan.rewrite_drizzle_cfg:
                    actions.extend(apply_drizzle_cfg_rewrite(ws))
                for p in plan.fix_font_display:
                    actions.extend(apply_font_display_fix(p))
                if plan.flag_raw_sqlite:
                    actions.extend(apply_raw_sqlite_flag(ws))
            else:
                if plan.move_scaffold:
                    actions.append("would move src/lib/db → lib/db and src/app/api/items → app/api/items")
                if plan.rewrite_drizzle_cfg:
                    actions.append("would rewrite drizzle.config.ts schema path")
                for p in plan.fix_font_display:
                    actions.append(f"would add display:'swap' to {p.relative_to(ws)}")
                if plan.flag_raw_sqlite:
                    actions.append("would write REPAIR.md flag for raw better-sqlite3")
        except Exception as e:
            failures += 1
            actions.append(f"ERROR: {e}")

        summary["workspaces"].append({
            "workspace": str(rel),
            "layout":    plan.layout,
            "actions":   actions,
        })

    print(json.dumps(summary, indent=2))
    print(
        f"\n{'APPLIED' if args.apply else 'DRY RUN'} — "
        f"{len(targets)} workspaces inspected, {failures} failures.",
        file=sys.stderr,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
