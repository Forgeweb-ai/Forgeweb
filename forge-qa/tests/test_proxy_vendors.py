"""
Verification for the proxy's Moonshot + Google adapters.

Mirrors test_proxy_sse.py — feeds the parsers exact SSE shapes the vendors
return, asserts usage normalization + cost wiring. No network. No real key.

Run:
    python3 forge-qa/tests/test_proxy_vendors.py
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "forge-llm-proxy"))
sys.path.insert(0, str(ROOT / "forge-qa" / "src"))

os.environ["FORGE_QA_PATH"] = str(ROOT / "forge-qa")

import proxy  # noqa: E402  — module under test


def _close(a: float, b: float, tol: float = 1e-4) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# ── Moonshot (OpenAI-compatible) SSE fixture ─────────────────────────────────
# Hand math for kimi-k2.6 ($0.95 in, $4 out, $0.16 cache_read):
#   prompt_tokens             = 100_000  (full prompt; 40K of it is cached)
#   prompt_cache_hit_tokens   =  40_000  → cache_read_usd  = 0.040 * 0.16 = 0.0064
#   non-cached input          =  60_000  → input_usd       = 0.060 * 0.95 = 0.057
#   completion_tokens         =  20_000  → output_usd      = 0.020 * 4    = 0.080
#   total                                              0.057 + 0.080 + 0.0064 = 0.1434
MOONSHOT_SSE = [
    'data: {"id":"cmpl-1","object":"chat.completion.chunk","model":"kimi-k2.6","choices":'
    '[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
    '',
    'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{"content":"Hi "},"finish_reason":null}]}',
    '',
    'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{"content":"there."},"finish_reason":null}]}',
    '',
    'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{"tool_calls":'
    '[{"index":0,"id":"call_1","type":"function","function":{"name":"bash","arguments":""}}]},"finish_reason":null}]}',
    '',
    'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{"tool_calls":'
    '[{"index":0,"function":{"arguments":"{\\"command\\":\\"ls\\"}"}}]},"finish_reason":null}]}',
    '',
    'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],'
    '"usage":{"prompt_tokens":100000,"completion_tokens":20000,"total_tokens":120000,'
    '"prompt_cache_hit_tokens":40000,"prompt_cache_miss_tokens":60000}}',
    '',
    'data: [DONE]',
    '',
]


def test_moonshot_parses_text() -> None:
    s = proxy._parse_openai_sse(MOONSHOT_SSE)
    assert s["text_blocks"] == ["Hi there."], s["text_blocks"]
    print(f"OK  moonshot text: {s['text_blocks']}")


def test_moonshot_parses_tool_call() -> None:
    s = proxy._parse_openai_sse(MOONSHOT_SSE)
    assert len(s["tool_uses"]) == 1, s["tool_uses"]
    tu = s["tool_uses"][0]
    assert tu["name"] == "bash", tu
    assert '"command":"ls"' in tu["input"], tu
    print(f"OK  moonshot tool_call: {tu['name']} input={tu['input']}")


def test_moonshot_normalizes_usage_cache_subtraction() -> None:
    """
    Moonshot reports prompt_tokens=100K AND prompt_cache_hit_tokens=40K.
    Our normalizer must give input_tokens=60K (the chargeable portion),
    output=20K, cache_read=40K.
    """
    s = proxy._parse_openai_sse(MOONSHOT_SSE)
    u = s["usage"]
    assert u["input_tokens"]              == 60_000, u
    assert u["output_tokens"]             == 20_000, u
    assert u["cache_read_input_tokens"]   == 40_000, u
    print(f"OK  moonshot usage normalized: in=60K out=20K cache_read=40K")


def test_moonshot_cost_matches_hand_math() -> None:
    s = proxy._parse_openai_sse(MOONSHOT_SSE)
    cost = proxy._compute_cost("kimi-k2.6", s["usage"])
    assert cost is not None
    assert _close(cost["input_usd"],      0.057),  cost
    assert _close(cost["output_usd"],     0.080),  cost
    assert _close(cost["cache_read_usd"], 0.0064), cost
    assert _close(cost["total_usd"],      0.1434), cost
    print(f"OK  kimi-k2.6 cost: total=${cost['total_usd']:.4f} "
          f"(in=${cost['input_usd']:.3f} out=${cost['output_usd']:.3f} cR=${cost['cache_read_usd']:.4f})")


def test_moonshot_nonstream_parses() -> None:
    """Non-streaming Moonshot response has usage in the top-level dict."""
    resp = {
        "id": "cmpl-2",
        "model": "kimi-k2.6",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hi there."},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 100_000, "completion_tokens": 20_000, "total_tokens": 120_000,
            "prompt_cache_hit_tokens": 40_000,
        },
    }
    s = proxy._summarise_openai_nonstream(resp)
    assert s["text_blocks"] == ["Hi there."], s
    assert s["stop_reason"] == "stop", s
    assert s["usage"]["input_tokens"]            == 60_000, s
    assert s["usage"]["cache_read_input_tokens"] == 40_000, s
    print(f"OK  moonshot non-stream: {s['stop_reason']} in={s['usage']['input_tokens']}")


# ── Google (Gemini) SSE fixture ──────────────────────────────────────────────
# Hand math for gemini-3.5-flash ($1.50 in, $9 out, $0.15 cache_read):
#   promptTokenCount         = 200_000 (full prompt; 50K cached)
#   cachedContentTokenCount  =  50_000 → cache_read_usd = 0.050 * 0.15 = 0.0075
#   non-cached input         = 150_000 → input_usd      = 0.150 * 1.50 = 0.225
#   candidatesTokenCount     =  30_000 → output_usd     = 0.030 * 9    = 0.270
#   total                                            0.225 + 0.270 + 0.0075 = 0.5025
GOOGLE_SSE = [
    'data: {"candidates":[{"content":{"parts":[{"text":"Hello "}],"role":"model"},"index":0}],'
    '"modelVersion":"gemini-3.5-flash"}',
    '',
    'data: {"candidates":[{"content":{"parts":[{"text":"from Gemini."}],"role":"model"},"index":0}]}',
    '',
    'data: {"candidates":[{"content":{"parts":[{"functionCall":{"name":"search","args":{"q":"forge"}}}],"role":"model"},'
    '"finishReason":"STOP","index":0}],'
    '"usageMetadata":{"promptTokenCount":200000,"candidatesTokenCount":30000,'
    '"totalTokenCount":230000,"cachedContentTokenCount":50000},'
    '"modelVersion":"gemini-3.5-flash"}',
    '',
]


def test_google_parses_text() -> None:
    s = proxy._parse_google_sse(GOOGLE_SSE)
    assert s["text_blocks"] == ["Hello from Gemini."], s["text_blocks"]
    print(f"OK  google text: {s['text_blocks']}")


def test_google_parses_function_call() -> None:
    s = proxy._parse_google_sse(GOOGLE_SSE)
    assert len(s["tool_uses"]) == 1, s["tool_uses"]
    tu = s["tool_uses"][0]
    assert tu["name"] == "search", tu
    assert '"q": "forge"' in tu["input"] or '"q":"forge"' in tu["input"], tu
    print(f"OK  google function_call: {tu['name']} input={tu['input']}")


def test_google_parses_finish_reason() -> None:
    s = proxy._parse_google_sse(GOOGLE_SSE)
    assert s["stop_reason"] == "STOP", s
    print(f"OK  google finishReason: {s['stop_reason']}")


def test_google_normalizes_usage_cache_subtraction() -> None:
    s = proxy._parse_google_sse(GOOGLE_SSE)
    u = s["usage"]
    assert u["input_tokens"]            == 150_000, u
    assert u["output_tokens"]           ==  30_000, u
    assert u["cache_read_input_tokens"] ==  50_000, u
    print(f"OK  google usage normalized: in=150K out=30K cache_read=50K")


def test_google_cost_matches_hand_math() -> None:
    s = proxy._parse_google_sse(GOOGLE_SSE)
    cost = proxy._compute_cost("gemini-3.5-flash", s["usage"])
    assert cost is not None
    assert _close(cost["input_usd"],      0.225),  cost
    assert _close(cost["output_usd"],     0.270),  cost
    assert _close(cost["cache_read_usd"], 0.0075), cost
    assert _close(cost["total_usd"],      0.5025), cost
    print(f"OK  gemini-3.5-flash cost: total=${cost['total_usd']:.4f} "
          f"(in=${cost['input_usd']:.3f} out=${cost['output_usd']:.3f} cR=${cost['cache_read_usd']:.4f})")


def test_google_nonstream_parses() -> None:
    resp = {
        "candidates": [{
            "content": {"parts": [{"text": "Hello from Gemini."}], "role": "model"},
            "finishReason": "STOP", "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount": 200_000,
            "candidatesTokenCount": 30_000,
            "totalTokenCount": 230_000,
            "cachedContentTokenCount": 50_000,
        },
        "modelVersion": "gemini-3.5-flash",
    }
    s = proxy._summarise_google_nonstream(resp)
    assert s["text_blocks"] == ["Hello from Gemini."], s
    assert s["stop_reason"] == "STOP", s
    assert s["usage"]["input_tokens"]            == 150_000, s
    assert s["usage"]["cache_read_input_tokens"] ==  50_000, s
    print(f"OK  google non-stream: stop={s['stop_reason']}")


# ── Route registration smoke (no real upstream needed) ───────────────────────

def test_routes_registered() -> None:
    """Every vendor's deep-logging route must be wired up."""
    paths = {(r.path, m) for r in proxy.app.routes for m in getattr(r, "methods", set()) or []}
    expected = [
        ("/v1/messages",                                  "POST"),
        ("/moonshot/v1/chat/completions",                 "POST"),
        ("/google/v1beta/models/{model_action:path}",     "POST"),
        ("/v1/{path:path}",                               "POST"),
        ("/moonshot/{path:path}",                         "POST"),
        ("/google/{path:path}",                           "POST"),
        ("/healthz",                                      "GET"),
    ]
    for p, m in expected:
        assert (p, m) in paths, f"missing route {m} {p}"
    print(f"OK  all {len(expected)} routes registered")


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
