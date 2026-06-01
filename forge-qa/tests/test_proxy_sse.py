"""
Verification for the proxy's SSE parser + cost wiring.

We don't spin up FastAPI — we import the proxy module directly and feed its
parser the exact SSE event shape Anthropic produces. This proves the gzip-fix
+ usage capture + cost computation work, before we point any real key at it.

Run:
    python3 forge-qa/tests/test_proxy_sse.py
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "forge-llm-proxy"))
sys.path.insert(0, str(ROOT / "forge-qa" / "src"))

# Pin the rate card path so the proxy's lazy import finds it.
os.environ["FORGE_QA_PATH"] = str(ROOT / "forge-qa")

import proxy  # noqa: E402  — module under test


def _close(a: float, b: float, tol: float = 1e-4) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# ── Fixture: a realistic Anthropic SSE stream ────────────────────────────────
# Mirrors what the real /v1/messages endpoint returns for a streaming call
# with one text block and a final tool use. Token counts chosen so the cost
# math is easy to verify by hand against the Sonnet 4.6 rate card.
#
# Hand math for Sonnet 4.6 ($3/$15/$0.30/$3.75 per 1M):
#   input       = 0.150 * 3       = 0.450
#   output      =  0.040 * 15     = 0.600
#   cache_read  =  0.080 * 0.30   = 0.024
#   cache_write =  0.010 * 3.75   = 0.0375
#   total                          = 1.1115
SSE_LINES = [
    'event: message_start',
    'data: {"type":"message_start","message":{"id":"msg_01","type":"message",'
    '"role":"assistant","model":"claude-sonnet-4-6","content":[],"stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":150000,"output_tokens":1,'
    '"cache_creation_input_tokens":10000,"cache_read_input_tokens":80000}}}',
    '',
    'event: content_block_start',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    '',
    'event: content_block_delta',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello "}}',
    '',
    'event: content_block_delta',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"world."}}',
    '',
    'event: content_block_stop',
    'data: {"type":"content_block_stop","index":0}',
    '',
    'event: content_block_start',
    'data: {"type":"content_block_start","index":1,"content_block":'
    '{"type":"tool_use","id":"toolu_01","name":"bash","input":{}}}',
    '',
    'event: content_block_delta',
    'data: {"type":"content_block_delta","index":1,"delta":'
    '{"type":"input_json_delta","partial_json":"{\\"command\\":\\"ls\\"}"}}',
    '',
    'event: message_delta',
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},'
    '"usage":{"output_tokens":40000}}',
    '',
    'event: message_stop',
    'data: {"type":"message_stop"}',
    '',
    '',
]


def test_parser_extracts_text() -> None:
    summary = proxy._summarise_response_event(SSE_LINES)
    assert summary["text_blocks"] == ["Hello world."], summary["text_blocks"]
    print(f"OK  text_blocks extracted: {summary['text_blocks']}")


def test_parser_extracts_tool_use() -> None:
    summary = proxy._summarise_response_event(SSE_LINES)
    assert len(summary["tool_uses"]) == 1, summary["tool_uses"]
    tu = summary["tool_uses"][0]
    assert tu["name"] == "bash", tu
    assert '"command":"ls"' in tu["input"], tu
    print(f"OK  tool_use captured: {tu['name']} input={tu['input']}")


def test_parser_extracts_stop_reason() -> None:
    summary = proxy._summarise_response_event(SSE_LINES)
    assert summary["stop_reason"] == "tool_use", summary["stop_reason"]
    print(f"OK  stop_reason captured: {summary['stop_reason']}")


def test_parser_merges_usage_from_start_and_delta() -> None:
    """
    Anthropic puts input_tokens in message_start.message.usage and
    output_tokens in the cumulative message_delta.usage. The parser must
    merge both into one usage dict — that was the OLD bug.
    """
    summary = proxy._summarise_response_event(SSE_LINES)
    u = summary["usage"]
    assert u is not None, "usage was None — message_start was missed"
    assert u["input_tokens"]                == 150_000, u
    assert u["output_tokens"]               ==  40_000, u
    assert u["cache_creation_input_tokens"] ==  10_000, u
    assert u["cache_read_input_tokens"]     ==  80_000, u
    print(f"OK  usage merged: in={u['input_tokens']} out={u['output_tokens']} "
          f"cache_w={u['cache_creation_input_tokens']} cache_r={u['cache_read_input_tokens']}")


def test_cost_computation_matches_hand_math() -> None:
    summary = proxy._summarise_response_event(SSE_LINES)
    cost = proxy._compute_cost("claude-sonnet-4-6", summary["usage"])
    assert cost is not None, "cost was None — rate_card import probably failed"
    # Hand math:
    assert _close(cost["input_usd"],       0.450),   cost
    assert _close(cost["output_usd"],      0.600),   cost
    assert _close(cost["cache_read_usd"],  0.024),   cost
    assert _close(cost["cache_write_usd"], 0.0375),  cost
    assert _close(cost["total_usd"],       1.1115),  cost
    print(f"OK  cost computed: total=${cost['total_usd']:.4f} "
          f"(in=${cost['input_usd']:.3f} out=${cost['output_usd']:.3f} "
          f"cR=${cost['cache_read_usd']:.4f} cW=${cost['cache_write_usd']:.4f})")


def test_unknown_model_does_not_crash() -> None:
    """The proxy must keep working even if a model isn't in the rate card."""
    cost = proxy._compute_cost("future-model-9000", {"input_tokens": 100, "output_tokens": 100})
    assert cost is None
    print("OK  unknown model returns None instead of raising")


def test_missing_usage_returns_none() -> None:
    cost = proxy._compute_cost("claude-sonnet-4-6", None)
    assert cost is None
    print("OK  missing usage returns None")


def test_one_line_includes_cost() -> None:
    summary = proxy._summarise_response_event(SSE_LINES)
    cost = proxy._compute_cost("claude-sonnet-4-6", summary["usage"])
    line = proxy._summarise_response_one_line(summary, cost)
    assert "in=150000" in line, line
    assert "out=40000" in line, line
    assert "cost=$1.1115" in line, line
    print(f"OK  log line: {line}")


def test_proxy_imports_rate_card() -> None:
    """Proxy must have imported forge_qa rate card on startup."""
    assert proxy._rate_card is not None, (
        "proxy._rate_card is None — FORGE_QA_PATH or the relative fallback didn't resolve. "
        "The proxy will run but never log cost."
    )
    print(f"OK  rate_card imported by proxy from {proxy._FORGE_QA}")


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
