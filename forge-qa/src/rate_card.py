"""
forge_qa.rate_card
==================
Pure-Python cost computer. Zero deps beyond stdlib so EVERY consumer
(forge-llm-proxy, forge-qa harness, forge-server BYOK dashboard,
Platform-managed margin tracker) can import this same module and produce
the same number for the same usage.

One source of truth = `forge-qa/config/rate_card.json`. Update prices there,
not in code.

Usage
-----
    from forge_qa.rate_card import load, cost_usd, context_pressure

    rc = load()  # cached after first call

    # 487K input, 92K output, 11K cache write, 320K cache read on Sonnet 4.6
    c = cost_usd(
        model="claude-sonnet-4-6",
        input_tokens=487_000,
        output_tokens=92_000,
        cache_read_tokens=320_000,
        cache_write_tokens=11_000,
    )
    # → CostBreakdown(input_usd=0.501, output_usd=1.380, cache_read_usd=0.096,
    #                 cache_write_usd=0.041, total_usd=2.018, model=...)

    # "you're sending 78.4% of available context"
    p = context_pressure("claude-sonnet-4-6", sent_tokens=156_800)
    # → 0.784

Normalization for tokenizer drift
---------------------------------
Opus 4.7 has a new tokenizer that inflates counts ~35% vs Sonnet on
identical English. When comparing "same work, different model," divide
the observed token count by `tokenizer_inflate_factor` to get a baseline
count that's comparable across models:

    baseline = normalize_to_baseline("claude-opus-4-7", observed_tokens=270_000)
    # → 200_000 (what Sonnet would have seen for the same English)

Otherwise, Opus looks artificially expensive even when it's the right
choice for the task.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────
_HERE          = Path(__file__).resolve().parent
_RATE_CARD_DEFAULT = _HERE.parent / "config" / "rate_card.json"


# ── Data shapes ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CostBreakdown:
    """Per-call cost split, all in USD. `total_usd` is the sum of the rest."""
    model:           str
    input_usd:       float
    output_usd:      float
    cache_read_usd:  float
    cache_write_usd: float
    total_usd:       float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Loading ──────────────────────────────────────────────────────────────────

@lru_cache(maxsize=4)
def load(path: str | Path | None = None) -> dict[str, Any]:
    """Load and cache the rate card. Pass a custom path for tests."""
    p = Path(path) if path else _RATE_CARD_DEFAULT
    with p.open() as f:
        data = json.load(f)
    if "models" not in data:
        raise ValueError(f"rate card at {p} missing 'models' key")
    return data


def get_model(model: str, *, rate_card: dict[str, Any] | None = None) -> dict[str, Any]:
    """Look up a model entry. Raises KeyError with the available list if unknown."""
    rc = rate_card or load()
    models = rc["models"]
    if model not in models:
        raise KeyError(
            f"unknown model {model!r}. Known: {sorted(models)}"
        )
    return models[model]


# ── Cost computation ─────────────────────────────────────────────────────────

def cost_usd(
    model:              str,
    input_tokens:       int = 0,
    output_tokens:      int = 0,
    cache_read_tokens:  int = 0,
    cache_write_tokens: int = 0,
    *,
    rate_card: dict[str, Any] | None = None,
) -> CostBreakdown:
    """
    Compute USD cost for one call.

    Notes
    -----
    - `input_tokens` is the NON-cached prompt portion. If the vendor reports a
      separate cache_read field (Anthropic does), pass it as `cache_read_tokens`
      and do NOT double-count it in `input_tokens`.
    - `cache_write_tokens` covers Anthropic's `cache_creation_input_tokens`
      (premium charge for putting something in the cache). Where the vendor
      doesn't charge separately for cache writes (Google), this is ignored.
    - All token args must be non-negative. Negative passes raise ValueError —
      a bug in the meter, not a real number.
    """
    for name, n in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("cache_read_tokens", cache_read_tokens),
        ("cache_write_tokens", cache_write_tokens),
    ):
        if n < 0:
            raise ValueError(f"{name}={n} must be >= 0")

    m = get_model(model, rate_card=rate_card)

    input_usd       = (input_tokens       / 1_000_000) * m["input"]
    output_usd      = (output_tokens      / 1_000_000) * m["output"]
    cache_read_usd  = (cache_read_tokens  / 1_000_000) * (m.get("cache_read") or 0.0)
    cache_write_usd = (cache_write_tokens / 1_000_000) * (m.get("cache_write") or 0.0)

    total = input_usd + output_usd + cache_read_usd + cache_write_usd
    return CostBreakdown(
        model           = model,
        input_usd       = round(input_usd, 6),
        output_usd      = round(output_usd, 6),
        cache_read_usd  = round(cache_read_usd, 6),
        cache_write_usd = round(cache_write_usd, 6),
        total_usd       = round(total, 6),
    )


# ── Context-window utilization ───────────────────────────────────────────────

def context_pressure(
    model: str,
    sent_tokens: int,
    *,
    rate_card: dict[str, Any] | None = None,
) -> float:
    """
    Fraction of the model's context window already in use by the prompt.

    Useful for the "you're at 78% of your window" indicator — past ~0.66
    response quality degrades from noise, well before the hard cap.
    """
    if sent_tokens < 0:
        raise ValueError(f"sent_tokens={sent_tokens} must be >= 0")
    m = get_model(model, rate_card=rate_card)
    window = m["context_window"]
    return round(sent_tokens / window, 4)


def output_headroom(
    model: str,
    requested_max: int | None = None,
    *,
    rate_card: dict[str, Any] | None = None,
) -> int:
    """
    Return the achievable output token cap — min(requested, model's max).
    None for `requested_max` means "no caller-imposed cap, use the model's."
    """
    m = get_model(model, rate_card=rate_card)
    hard_cap = m["max_output_tokens"]
    if requested_max is None:
        return hard_cap
    if requested_max < 0:
        raise ValueError(f"requested_max={requested_max} must be >= 0")
    return min(requested_max, hard_cap)


# ── Tokenizer normalization ──────────────────────────────────────────────────

def normalize_to_baseline(
    model: str,
    observed_tokens: int,
    *,
    rate_card: dict[str, Any] | None = None,
) -> float:
    """
    Divide observed tokens by this model's inflation factor to get a
    "baseline" count comparable across models. Sonnet 4.6 is the baseline
    (factor 1.0). Opus 4.7 ~1.35 → 270K observed → 200K baseline.

    Use this when comparing "same English, different model" — otherwise
    a higher-inflation tokenizer looks artificially expensive.
    """
    if observed_tokens < 0:
        raise ValueError(f"observed_tokens={observed_tokens} must be >= 0")
    m = get_model(model, rate_card=rate_card)
    f = m.get("tokenizer_inflate_factor", 1.0)
    if f <= 0:
        raise ValueError(f"tokenizer_inflate_factor for {model} is {f}, must be > 0")
    return round(observed_tokens / f, 2)


# ── Convenience: estimate from a usage dict ──────────────────────────────────

def cost_from_usage(
    model: str,
    usage: dict[str, Any],
    *,
    rate_card: dict[str, Any] | None = None,
) -> CostBreakdown:
    """
    Compute cost directly from the `usage` dict shapes both Anthropic and
    Google return. Tolerates missing keys — treats them as 0. This is the
    one to call from the proxy and the harness.

    Anthropic shape:
        {"input_tokens": N, "output_tokens": M,
         "cache_creation_input_tokens": X, "cache_read_input_tokens": Y}

    Google shape (cleaned by caller into normalized keys; raw varies):
        {"input_tokens": N, "output_tokens": M, "cache_read_input_tokens": Y}
    """
    return cost_usd(
        model              = model,
        input_tokens       = int(usage.get("input_tokens", 0) or 0),
        output_tokens      = int(usage.get("output_tokens", 0) or 0),
        cache_read_tokens  = int(usage.get("cache_read_input_tokens", 0) or 0),
        cache_write_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0),
        rate_card          = rate_card,
    )


# ── CLI: quick check from terminal ───────────────────────────────────────────

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Compute cost for a call. Example: "
                    "python -m forge_qa.rate_card --model claude-sonnet-4-6 --in 500000 --out 100000"
    )
    p.add_argument("--model",     required=True)
    p.add_argument("--in",        dest="input_tokens",  type=int, default=0)
    p.add_argument("--out",       dest="output_tokens", type=int, default=0)
    p.add_argument("--cache-read",  dest="cache_read",  type=int, default=0)
    p.add_argument("--cache-write", dest="cache_write", type=int, default=0)
    args = p.parse_args()

    b = cost_usd(
        model              = args.model,
        input_tokens       = args.input_tokens,
        output_tokens      = args.output_tokens,
        cache_read_tokens  = args.cache_read,
        cache_write_tokens = args.cache_write,
    )
    print(json.dumps(b.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
