# forge-qa

Test harness for measuring Forge's per-model cost, latency, and quality across
the slots that make up one app build (design, build, fixer, chat, image).

**Status:** Phase 0 in progress — rate card + cost computer landed and verified.
Meter, runner, judge, and matrix are next.

---

## Why this exists

Forge runs two SKUs:
- **BYOK** — user pays the provider directly. They need a *transparent receipt*
  of input/output/cache tokens and dollar cost per session.
- **Platform-managed** — Forge charges a flat credit fee and eats inference.
  Margin is entirely a function of routing the right slot to the right model.
  Forge needs *data* to pick the right router.

Both SKUs share one meter. This directory is where that meter, the routing
strategies under test, and the scenarios that exercise them all live.

## Layout

```
forge-qa/
├── config/
│   └── rate_card.json          # single source of truth for prices + limits
├── src/
│   └── rate_card.py            # pure-Python cost computer (zero deps)
├── tests/
│   └── test_rate_card.py       # hand-computed assertions
├── scenarios/                  # pinned (model-agnostic) build prompts — TBD
├── fixtures/                   # mockup images, seed workspaces — TBD
├── rubric/                     # standard + premium quality rules — TBD
└── results/                    # per-run output (CSV + markdown) — TBD
```

## Quick start

```bash
# unit tests
python3 forge-qa/tests/test_rate_card.py

# CLI cost calc for one call
python3 -m forge_qa.rate_card --model claude-sonnet-4-6 --in 500000 --out 100000
# → {"total_usd": 3.0, ...}
```

## What lives where

| Concern | File |
|---|---|
| Update a model price | `config/rate_card.json` |
| Add a new model | `config/rate_card.json` + add test row in `tests/test_rate_card.py` |
| Compute cost from a usage dict | `cost_from_usage()` in `src/rate_card.py` |
| "How much of the window am I using?" | `context_pressure()` in `src/rate_card.py` |
| Compare token counts across tokenizers | `normalize_to_baseline()` in `src/rate_card.py` |

## Design rules this directory follows

- **One rate card, every consumer reads it.** Proxy meter, harness, BYOK
  dashboard, Platform-managed margin tracker all `from forge_qa.rate_card import
  cost_from_usage`. Never hard-code a price in code.
- **Pure Python, no deps.** So the proxy (FastAPI) and forge-server (FastAPI)
  can import the same module without dragging in test infra.
- **Hand-computed assertions.** Every price gets a hand-math test. When a
  vendor moves a number, the test fails — that's the signal to update the card.
- **Negative tokens raise.** Silent zeros hide meter bugs. Loud failures don't.
- **Unknown model names raise with the known list.** Typos cost money;
  actionable errors are cheap.
