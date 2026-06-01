"""
forge_qa.meter
==============
Aggregate cost/tokens from the proxy's per-call JSON logs.

Each call lives at:
    forge-llm-proxy-logs/calls/<YYYYMMDD_HHMMSS>_<call_id>.json

After the proxy gzip-fix lands, every NEW file contains:
    record["model"]                       # e.g. "claude-sonnet-4-6"
    record["cost"]                        # {input_usd, output_usd, ..., total_usd}
    record["response"]["summary"]["usage"]  # raw token counts
    record["elapsed_ms"]
    record["request"]["headers"]["x-session-affinity"]  # opencode session id

This module walks those files and rolls them up.

Historical calls (before the fix) have `cost=None` and unusable raw_sse —
we skip them with a count in the summary so the user knows what's missing.

CLI examples
------------
    # Roll up everything, sorted by session
    python3 -m forge_qa.meter --root forge-llm-proxy-logs

    # Last 200 calls only
    python3 -m forge_qa.meter --root forge-llm-proxy-logs --last 200

    # One session
    python3 -m forge_qa.meter --root forge-llm-proxy-logs \\
        --session ses_199a87d86ffelUNfJ1h423BTbx
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# ── Data ─────────────────────────────────────────────────────────────────────

SLOTS_ORDER = ("design", "design_review", "build", "fixer", "chat", "unknown")


@dataclass
class CallRow:
    """One row per proxy log file."""
    path:        Path
    call_id:     str
    ts:          str
    session_id:  str | None
    model:       str | None
    slot:        str             # design | design_review | build | fixer | chat | unknown
    elapsed_ms: int
    input_tokens:  int
    output_tokens: int
    cache_read_tokens:  int
    cache_write_tokens: int
    total_usd:   float | None     # None = historical/broken call


@dataclass
class SlotRollup:
    slot:                str
    calls:               int           = 0
    models:              set[str]      = field(default_factory=set)
    input_tokens:        int           = 0
    output_tokens:       int           = 0
    cache_read_tokens:   int           = 0
    cache_write_tokens:  int           = 0
    total_usd:           float         = 0.0
    elapsed_ms:          int           = 0


@dataclass
class SessionRollup:
    session_id:  str | None
    calls:       int                       = 0
    models:      set[str]                  = field(default_factory=set)
    input_tokens:  int                     = 0
    output_tokens: int                     = 0
    cache_read_tokens:  int                = 0
    cache_write_tokens: int                = 0
    total_usd:   float                     = 0.0
    elapsed_ms:  int                       = 0
    missing_cost: int                      = 0          # calls with no cost data
    by_slot:     dict[str, SlotRollup]     = field(default_factory=dict)


# ── Parsing ──────────────────────────────────────────────────────────────────

def _safe_get(d: dict | None, *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def parse_call_file(path: Path) -> CallRow | None:
    """Read one call JSON. Returns None if the file is malformed."""
    try:
        rec = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    usage = _safe_get(rec, "response", "summary", "usage", default={}) or {}
    cost  = rec.get("cost") or {}
    sess  = _safe_get(rec, "request", "headers", "x-session-affinity")

    return CallRow(
        path                = path,
        call_id             = rec.get("call_id", path.stem),
        ts                  = rec.get("ts", ""),
        session_id          = sess,
        model               = rec.get("model"),
        slot                = rec.get("slot") or "unknown",
        elapsed_ms          = int(rec.get("elapsed_ms") or 0),
        input_tokens        = int(usage.get("input_tokens") or 0),
        output_tokens       = int(usage.get("output_tokens") or 0),
        cache_read_tokens   = int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens  = int(usage.get("cache_creation_input_tokens") or 0),
        total_usd           = cost.get("total_usd") if isinstance(cost, dict) else None,
    )


def iter_call_files(root: Path, *, last: int | None = None) -> Iterable[Path]:
    """Yield call JSON paths under root/calls/, oldest first.

    `last=N` returns the N most recent files only (cheaper for big log dirs).
    """
    calls = root / "calls"
    if not calls.is_dir():
        return iter(())
    files = sorted(p for p in calls.iterdir() if p.suffix == ".json")
    if last is not None and last >= 0:
        files = files[-last:]
    return iter(files)


# ── Rollups ──────────────────────────────────────────────────────────────────

def roll_up(rows: Iterable[CallRow]) -> dict[str | None, SessionRollup]:
    """Group rows by session_id (None = ungrouped). Also breaks down by slot."""
    out: dict[str | None, SessionRollup] = defaultdict(lambda: SessionRollup(session_id=None))
    for r in rows:
        s = out.setdefault(r.session_id, SessionRollup(session_id=r.session_id))
        s.calls              += 1
        if r.model:
            s.models.add(r.model)
        s.input_tokens       += r.input_tokens
        s.output_tokens      += r.output_tokens
        s.cache_read_tokens  += r.cache_read_tokens
        s.cache_write_tokens += r.cache_write_tokens
        s.elapsed_ms         += r.elapsed_ms
        if r.total_usd is None:
            s.missing_cost += 1
        else:
            s.total_usd += r.total_usd

        slot = s.by_slot.setdefault(r.slot, SlotRollup(slot=r.slot))
        slot.calls              += 1
        if r.model:
            slot.models.add(r.model)
        slot.input_tokens       += r.input_tokens
        slot.output_tokens      += r.output_tokens
        slot.cache_read_tokens  += r.cache_read_tokens
        slot.cache_write_tokens += r.cache_write_tokens
        slot.elapsed_ms         += r.elapsed_ms
        if r.total_usd is not None:
            slot.total_usd += r.total_usd
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────

def _fmt_usd(v: float) -> str:
    return f"${v:>9.4f}"


def _print_session_table(rollups: dict[str | None, SessionRollup], *, filter_session: str | None) -> None:
    sessions = list(rollups.values())
    if filter_session:
        sessions = [s for s in sessions if s.session_id == filter_session]
    sessions.sort(key=lambda s: s.total_usd, reverse=True)

    print(f"{'Session':<40} {'Calls':>6} {'In tok':>10} {'Out tok':>10} {'CacheR':>10} "
          f"{'Latency':>9} {'Cost':>11}  Models")
    print("-" * 130)

    grand = SessionRollup(session_id=None)
    grand.models = set()
    for s in sessions:
        sid = (s.session_id or "(no session)")[:40]
        models = ",".join(sorted(s.models)) or "?"
        latency_s = s.elapsed_ms / 1000
        miss = f"  [{s.missing_cost} no-cost]" if s.missing_cost else ""
        print(f"{sid:<40} {s.calls:>6} {s.input_tokens:>10} {s.output_tokens:>10} "
              f"{s.cache_read_tokens:>10} {latency_s:>8.1f}s {_fmt_usd(s.total_usd)}  {models}{miss}")
        grand.calls              += s.calls
        grand.input_tokens       += s.input_tokens
        grand.output_tokens      += s.output_tokens
        grand.cache_read_tokens  += s.cache_read_tokens
        grand.cache_write_tokens += s.cache_write_tokens
        grand.elapsed_ms         += s.elapsed_ms
        grand.total_usd          += s.total_usd
        grand.missing_cost       += s.missing_cost
        grand.models             |= s.models

    print("-" * 130)
    print(f"{'TOTAL':<40} {grand.calls:>6} {grand.input_tokens:>10} {grand.output_tokens:>10} "
          f"{grand.cache_read_tokens:>10} {grand.elapsed_ms/1000:>8.1f}s {_fmt_usd(grand.total_usd)}  "
          f"{','.join(sorted(grand.models)) or '?'}")
    if grand.missing_cost:
        print(f"\nNote: {grand.missing_cost} call(s) had no cost data — historical, pre-gzip-fix, "
              f"or unknown model. Reproduce or skip.")

    if filter_session and sessions:
        _print_slot_breakdown(sessions[0])


def _print_slot_breakdown(s: SessionRollup) -> None:
    print(f"\nPer-slot breakdown for session {s.session_id}:")
    print(f"  {'Slot':<14} {'Calls':>6} {'In tok':>10} {'Out tok':>10} {'CacheR':>10} "
          f"{'Latency':>9} {'Cost':>11}  Models")
    print("  " + "-" * 100)
    for slot_name in SLOTS_ORDER:
        slot = s.by_slot.get(slot_name)
        if not slot or slot.calls == 0:
            continue
        models = ",".join(sorted(slot.models)) or "?"
        print(f"  {slot.slot:<14} {slot.calls:>6} {slot.input_tokens:>10} {slot.output_tokens:>10} "
              f"{slot.cache_read_tokens:>10} {slot.elapsed_ms/1000:>8.1f}s {_fmt_usd(slot.total_usd)}  {models}")


# ── Compare view — multiple sessions side-by-side by slot ────────────────────

def _print_compare(rollups: dict[str | None, SessionRollup], session_ids: list[str]) -> None:
    """
    Side-by-side comparison: same prompt, different strategies. One column per
    session, one row per slot, plus a TOTAL row. Use case:
        meter --compare ses_sonnet ses_flash35 ses_kimi
    """
    cols: list[SessionRollup] = []
    for sid in session_ids:
        s = rollups.get(sid)
        if s is None:
            print(f"  ! session not found: {sid}")
            continue
        cols.append(s)
    if not cols:
        print("no matching sessions to compare")
        return

    # Header
    label_w = 14
    col_w   = 26
    header = f"{'Slot':<{label_w}}"
    sub    = f"{'':<{label_w}}"
    for s in cols:
        header += f"  {(s.session_id or '?')[:col_w-2]:<{col_w}}"
        sub    += f"  {'calls / cost / model':<{col_w}}"
    print(header)
    print(sub)
    print("-" * len(header))

    # Per-slot rows
    for slot_name in SLOTS_ORDER:
        line = f"{slot_name:<{label_w}}"
        any_data = False
        for s in cols:
            slot = s.by_slot.get(slot_name)
            if not slot or slot.calls == 0:
                line += f"  {'—':<{col_w}}"
                continue
            any_data = True
            models = ",".join(sorted(slot.models))[:14]
            cell = f"{slot.calls:>3} / ${slot.total_usd:>6.3f} / {models}"
            line += f"  {cell:<{col_w}}"
        if any_data:
            print(line)

    # TOTAL row
    print("-" * len(header))
    line = f"{'TOTAL':<{label_w}}"
    for s in cols:
        models = ",".join(sorted(s.models))[:14]
        cell = f"{s.calls:>3} / ${s.total_usd:>6.3f} / {models}"
        line += f"  {cell:<{col_w}}"
    print(line)

    # Pairwise delta vs first column
    if len(cols) > 1:
        base = cols[0]
        print(f"\nΔ vs {base.session_id} (negative = cheaper):")
        for s in cols[1:]:
            delta = s.total_usd - base.total_usd
            pct = (delta / base.total_usd * 100) if base.total_usd else 0
            sign = "+" if delta >= 0 else ""
            print(f"  {s.session_id:<40} {sign}${delta:>+8.4f}  ({sign}{pct:+.1f}%)")


def _cli() -> int:
    p = argparse.ArgumentParser(description="Aggregate cost/tokens from forge-llm-proxy logs.")
    p.add_argument("--root", default="forge-llm-proxy-logs",
                   help="Path to forge-llm-proxy-logs/ (default: ./forge-llm-proxy-logs)")
    p.add_argument("--last", type=int, default=None,
                   help="Only scan the N most recent call files")
    p.add_argument("--session", default=None,
                   help="Filter to a single x-session-affinity (also prints per-slot breakdown)")
    p.add_argument("--compare", nargs="+", default=None, metavar="SESSION_ID",
                   help="Side-by-side per-slot comparison of N sessions. "
                        "Use when you ran the same prompt against different strategies.")
    args = p.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not (root / "calls").is_dir():
        print(f"no calls/ directory under {root}", file=sys.stderr)
        return 1

    rows = [r for r in (parse_call_file(p) for p in iter_call_files(root, last=args.last)) if r]
    if not rows:
        print(f"no call files matched under {root}")
        return 0

    rollups = roll_up(rows)

    if args.compare:
        _print_compare(rollups, args.compare)
    else:
        _print_session_table(rollups, filter_session=args.session)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
