"""
Per-100-credit margin calculator.

PLACEHOLDER INPUTS — see SLOT_TOKENS below. These are unverified guesses.
Real numbers come from forge-llm-proxy logs once the gzip-decode bug is
fixed and we replay real sessions through the meter.

Revenue assumption: $25 per 100 credits (Lovable-equivalent Pro tier).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import rate_card as rc

REVENUE_PER_100 = 25.00

# ── Placeholder slot-level token totals per 100 credits ──────────────────────
# Mix: 30 heavy builds + 50 mid edits + 20 chat tweaks per 100 credits.
SLOT_TOKENS = {
    # slot:   (input_tokens_per_100_credits, output_tokens_per_100_credits)
    "design": (1_350_000,   300_000),
    "build":  (18_650_000,  3_700_000),
    "chat":   (400_000,     100_000),
    "fixer":  (0,           0),         # assume no errors in this mix (optimistic)
    "image":  (0,           0),         # assume no mockup inputs in this mix
}

# ── Routing strategies (slot → model) ────────────────────────────────────────
STRATEGIES = {
    "all_opus":        {"design": "claude-opus-4-7",   "build": "claude-opus-4-7",   "chat": "claude-opus-4-7",   "fixer": "claude-opus-4-7",   "image": "claude-opus-4-7"},
    "all_sonnet":      {"design": "claude-sonnet-4-6", "build": "claude-sonnet-4-6", "chat": "claude-sonnet-4-6", "fixer": "claude-sonnet-4-6", "image": "claude-sonnet-4-6"},
    "all_flash_3_5":   {"design": "gemini-3.5-flash",  "build": "gemini-3.5-flash",  "chat": "gemini-3.5-flash",  "fixer": "gemini-3.5-flash",  "image": "gemini-3.5-flash"},
    "all_flash_3":     {"design": "gemini-3-flash",    "build": "gemini-3-flash",    "chat": "gemini-3-flash",    "fixer": "gemini-3-flash",    "image": "gemini-3-flash"},
    "premium_routed":  {"design": "claude-opus-4-7",   "build": "claude-sonnet-4-6", "chat": "claude-haiku-4-5",  "fixer": "claude-sonnet-4-6", "image": "claude-sonnet-4-6"},
    "balanced_routed": {"design": "claude-sonnet-4-6", "build": "gemini-3.5-flash",  "chat": "claude-haiku-4-5",  "fixer": "claude-sonnet-4-6", "image": "gemini-3-flash"},
    "budget_routed":   {"design": "claude-sonnet-4-6", "build": "gemini-3-flash",    "chat": "claude-haiku-4-5",  "fixer": "claude-haiku-4-5",  "image": "gemini-3-flash"},
}


def cost_per_100_credits(strategy: dict[str, str]) -> tuple[float, dict[str, float]]:
    total = 0.0
    per_slot: dict[str, float] = {}
    for slot, (in_tok, out_tok) in SLOT_TOKENS.items():
        if in_tok == 0 and out_tok == 0:
            per_slot[slot] = 0.0
            continue
        b = rc.cost_usd(
            model         = strategy[slot],
            input_tokens  = in_tok,
            output_tokens = out_tok,
        )
        per_slot[slot] = b.total_usd
        total += b.total_usd
    return round(total, 2), per_slot


def main() -> None:
    print(f"Revenue per 100 credits:  ${REVENUE_PER_100:.2f}\n")
    print(f"{'Strategy':<18} {'Cost':>8} {'Margin':>9} {'Margin %':>9}   per-slot (design/build/chat)")
    print("-" * 100)
    rows = []
    for name, mapping in STRATEGIES.items():
        cost, per_slot = cost_per_100_credits(mapping)
        margin     = REVENUE_PER_100 - cost
        margin_pct = round(100 * margin / REVENUE_PER_100, 1)
        rows.append((name, cost, margin, margin_pct, per_slot))
    rows.sort(key=lambda r: r[1])  # cheapest first
    for name, cost, margin, margin_pct, per_slot in rows:
        sign = "+" if margin >= 0 else ""
        ps   = f"${per_slot['design']:>5.2f} / ${per_slot['build']:>6.2f} / ${per_slot['chat']:>5.2f}"
        print(f"{name:<18} ${cost:>7.2f} {sign}${margin:>7.2f} {margin_pct:>8.1f}%   {ps}")
    print()
    print("INPUTS ARE PLACEHOLDERS. Replace SLOT_TOKENS with real measured values")
    print("once forge-llm-proxy captures usage correctly.")


if __name__ == "__main__":
    main()
