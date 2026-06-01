"""
Verification tests for forge_qa.rate_card.

Each hand-computed number is derived from the published rate card:
    cost_input_usd  = (input_tokens  / 1_000_000) * input_price
    cost_output_usd = (output_tokens / 1_000_000) * output_price

If a test fails after a rate card update, the rate card moved — fix the
expected number, not the math.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

# Make sibling package importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import rate_card as rc


def _close(a: float, b: float, tol: float = 1e-4) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# ── Hand-computed cases ──────────────────────────────────────────────────────

CASES: list[tuple[str, str, int, int, float, float, float]] = [
    # (label,                          model,                in,       out,     expect_in, expect_out, expect_total)
    ("Sonnet 4.6 — typical build",     "claude-sonnet-4-6",  500_000,  100_000, 1.50,      1.50,       3.00),
    ("Opus 4.7 — same call",           "claude-opus-4-7",    500_000,  100_000, 2.50,      2.50,       5.00),
    ("Gemini 3.5 Flash — same call",   "gemini-3.5-flash",   500_000,  100_000, 0.75,      0.90,       1.65),
    ("Gemini 3 Flash — same call",     "gemini-3-flash",     500_000,  100_000, 0.25,      0.30,       0.55),
    ("Haiku 4.5 — chat-sized",         "claude-haiku-4-5",   8_000,    2_000,   0.008,     0.010,      0.018),
    ("GPT-4o-mini — chat-sized",       "gpt-4o-mini",        8_000,    2_000,   0.0012,    0.0012,     0.0024),
]


def test_hand_computed_costs() -> None:
    for label, model, n_in, n_out, e_in, e_out, e_total in CASES:
        b = rc.cost_usd(model, input_tokens=n_in, output_tokens=n_out)
        assert _close(b.input_usd,  e_in),  f"{label}: input  {b.input_usd}  != {e_in}"
        assert _close(b.output_usd, e_out), f"{label}: output {b.output_usd} != {e_out}"
        assert _close(b.total_usd,  e_total), f"{label}: total {b.total_usd} != {e_total}"
        print(f"OK  {label:40s}  in=${b.input_usd:.4f}  out=${b.output_usd:.4f}  total=${b.total_usd:.4f}")


def test_cache_savings_sonnet() -> None:
    """
    Real Anthropic usage shape:
      487K NEW input, 92K output, 11K cache_creation, 320K cache_read.

    Hand math (Sonnet 4.6: in $3, out $15, cache_read $0.30, cache_write $3.75):
      input       = 0.487 * 3       = 1.461
      output      = 0.092 * 15      = 1.380
      cache_read  = 0.320 * 0.30    = 0.096
      cache_write = 0.011 * 3.75    = 0.04125
      total       = 2.97825 → rounded to 2.978
    """
    b = rc.cost_usd(
        model              = "claude-sonnet-4-6",
        input_tokens       = 487_000,
        output_tokens      = 92_000,
        cache_read_tokens  = 320_000,
        cache_write_tokens = 11_000,
    )
    assert _close(b.input_usd,       1.461),  b
    assert _close(b.output_usd,      1.380),  b
    assert _close(b.cache_read_usd,  0.096),  b
    assert _close(b.cache_write_usd, 0.04125), b
    assert _close(b.total_usd,       2.97825), b
    print(f"OK  Sonnet cache breakdown: total=${b.total_usd:.4f}")


def test_cost_from_usage_anthropic_shape() -> None:
    """Proxy will pass Anthropic's raw usage dict directly into cost_from_usage."""
    usage = {
        "input_tokens":                487_000,
        "output_tokens":               92_000,
        "cache_creation_input_tokens": 11_000,
        "cache_read_input_tokens":     320_000,
    }
    b = rc.cost_from_usage("claude-sonnet-4-6", usage)
    assert _close(b.total_usd, 2.97825), b
    print(f"OK  cost_from_usage matches direct call")


def test_missing_keys_are_zero() -> None:
    """A usage dict with only input + output (no cache keys) must not error."""
    b = rc.cost_from_usage("gemini-3.5-flash", {"input_tokens": 500_000, "output_tokens": 100_000})
    assert _close(b.total_usd, 1.65), b
    print(f"OK  missing cache keys treated as 0")


def test_negative_rejected() -> None:
    """Negative tokens means the meter is buggy — fail loudly, not silently."""
    try:
        rc.cost_usd("claude-sonnet-4-6", input_tokens=-5)
    except ValueError:
        print("OK  negative input rejected")
        return
    raise AssertionError("expected ValueError on negative tokens")


def test_unknown_model_lists_known() -> None:
    """Typos should produce an actionable error, not a $0 charge."""
    try:
        rc.cost_usd("claude-sonet-4-6", input_tokens=100)  # typo
    except KeyError as e:
        msg = str(e)
        assert "claude-sonnet-4-6" in msg, f"error must list known models: {msg}"
        print("OK  unknown model lists candidates")
        return
    raise AssertionError("expected KeyError on unknown model")


def test_context_pressure() -> None:
    p = rc.context_pressure("claude-sonnet-4-6", sent_tokens=156_800)
    # 156800 / 200000 = 0.784
    assert _close(p, 0.784), p
    p2 = rc.context_pressure("gemini-3.5-flash", sent_tokens=156_800)
    # 156800 / 1048576 ≈ 0.1495
    assert _close(p2, 0.1495, tol=1e-3), p2
    print(f"OK  context pressure Sonnet=0.784  Gemini=0.150")


def test_output_headroom() -> None:
    assert rc.output_headroom("claude-opus-4-7") == 32000
    assert rc.output_headroom("claude-opus-4-7", requested_max=16000) == 16000
    assert rc.output_headroom("claude-opus-4-7", requested_max=999_999) == 32000
    print("OK  output headroom honors min(requested, model_max)")


def test_tokenizer_normalization() -> None:
    """Opus 4.7 reports ~270K for the same English Sonnet sees as 200K."""
    baseline = rc.normalize_to_baseline("claude-opus-4-7", observed_tokens=270_000)
    assert _close(baseline, 200_000, tol=1.0), baseline
    # Sonnet baseline → identity
    same = rc.normalize_to_baseline("claude-sonnet-4-6", observed_tokens=200_000)
    assert same == 200_000
    print(f"OK  Opus 270K → {baseline} baseline; Sonnet identity")


# ── Driver ───────────────────────────────────────────────────────────────────

def _run_all() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERR  {t.__name__}: {e!r}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
