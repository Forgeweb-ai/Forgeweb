"""
forge-llm-proxy
================
Single chokepoint between opencode and every LLM provider Forge uses.
opencode is configured to send all traffic here; we log, compute cost, and
forward upstream to the right vendor.

Endpoints proxied (all log + capture cost):
  POST /v1/messages                                   → Anthropic
  POST /moonshot/v1/chat/completions                  → Moonshot (Kimi K2.6, OpenAI-compatible)
  POST /google/v1beta/models/{model:action}           → Google (Gemini Flash family)

Plus catch-alls that forward without deep logging:
  /v1/{path}        → Anthropic
  /moonshot/{path}  → Moonshot
  /google/{path}    → Google

Logging targets (per call):
  1. stdout one-liner (concise)
  2. forge-llm-proxy.log (JSONL, every record)
  3. forge-llm-proxy-logs/calls/<ts>_<id>.json (one file per call)

Every per-call file carries a `cost` field computed via forge-qa's rate card.
Vendor-specific usage shapes (Anthropic / OpenAI-compatible / Google) are
normalized to the rate-card shape before cost lookup:
    {input_tokens, output_tokens, cache_read_input_tokens,
     cache_creation_input_tokens}

Healthcheck:
  GET /healthz → {"ok": true}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

# ── forge-qa rate card import ────────────────────────────────────────────────
# Single source of truth for $/token lives at forge-qa/config/rate_card.json.
# Same JSON, same module imported by the proxy, the harness, and any future
# dashboard. NEVER hard-code a price in this file.
#
# Path resolution:
#   1. FORGE_QA_PATH env var (set in docker-compose to /forge-qa)
#   2. ../forge-qa relative to this file (local-dev fallback)
_FORGE_QA = Path(os.environ.get("FORGE_QA_PATH") or (Path(__file__).resolve().parent.parent / "forge-qa"))
if (_FORGE_QA / "src").is_dir():
    sys.path.insert(0, str(_FORGE_QA / "src"))
try:
    import rate_card as _rate_card  # type: ignore[import-not-found]
except ImportError:
    _rate_card = None  # proxy keeps working without cost computation

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_UPSTREAM = os.environ.get("ANTHROPIC_UPSTREAM", "https://api.anthropic.com")
MOONSHOT_UPSTREAM  = os.environ.get("MOONSHOT_UPSTREAM",  "https://api.moonshot.ai")
GOOGLE_UPSTREAM    = os.environ.get("GOOGLE_UPSTREAM",    "https://generativelanguage.googleapis.com")

LOG_DIR            = Path(os.environ.get("FORGE_LLM_PROXY_LOG_DIR", "./forge-llm-proxy-logs"))
LOG_FILE           = LOG_DIR / "forge-llm-proxy.log"
CALLS_DIR          = LOG_DIR / "calls"
LOG_BODY_MAX       = int(os.environ.get("FORGE_LLM_PROXY_BODY_MAX", "200000"))  # cap per-field body size in stdout
PORT               = int(os.environ.get("PORT", "7799"))

# ── Stream watchdog ──────────────────────────────────────────────────────────
# Idle-token timeout for streaming responses. If the upstream model goes
# silent for this many seconds without delivering a single byte, we abort
# the connection, emit a vendor-agnostic terminator, and record the turn
# as stalled. Without this, a stalled provider keepalive freezes opencode
# on "Thinking…" until something else kills the TCP socket — at 100k
# containers this is a guaranteed daily outage class.
#
# 45s is conservative: real model turns can have multi-second gaps between
# tool-result and final-message chunks; 45s comfortably exceeds the longest
# legitimate gap observed in production while still bounding the user-
# visible hang at well under one minute. Tunable via env without code
# change so operators can dial it down once we have telemetry.
_IDLE_TOKEN_TIMEOUT_S = float(os.environ.get("FORGE_LLM_PROXY_IDLE_TIMEOUT", "45"))

LOG_DIR.mkdir(parents=True, exist_ok=True)
CALLS_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("forge-llm-proxy")

app = FastAPI(title="forge-llm-proxy")

# Single httpx client — keep-alives + connection pool
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=30.0),
    follow_redirects=False,
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(s: str, n: int = LOG_BODY_MAX) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"…<truncated {len(s) - n} chars>"


def _summarise_request(body: dict) -> str:
    """Concise one-liner for stdout — works for Anthropic, OpenAI-compat, Google."""
    model     = body.get("model", "?")
    # Anthropic / OpenAI-compat use `messages`; Google uses `contents`.
    messages  = body.get("messages") or body.get("contents") or []
    system    = body.get("system") or body.get("systemInstruction")
    tools     = body.get("tools") or []
    stream    = body.get("stream", False)

    sys_len = 0
    if isinstance(system, str):
        sys_len = len(system)
    elif isinstance(system, dict):
        # Google's systemInstruction shape
        parts = system.get("parts") or []
        sys_len = sum(len(p.get("text", "")) for p in parts if isinstance(p, dict))
    elif isinstance(system, list):
        sys_len = sum(len((b.get("text") or "")) for b in system if isinstance(b, dict))

    last_user_hint = ""
    for m in reversed(messages):
        role = m.get("role")
        if role in ("user", None):  # Google's "user" role
            content = m.get("content") or m.get("parts")
            if isinstance(content, str):
                last_user_hint = content[:80].replace("\n", " ")
            elif isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        parts.append(b.get("text", ""))
                last_user_hint = " ".join(parts)[:80].replace("\n", " ")
            if last_user_hint:
                break

    tool_names = []
    for t in tools:
        if isinstance(t, dict):
            tool_names.append(t.get("name") or _safe_get(t, "function", "name"))
    has_task_tool = any(n == "task" for n in tool_names)

    return (
        f"model={model} msgs={len(messages)} system_len={sys_len} "
        f"tools={len(tools)}{'(+task)' if has_task_tool else ''} "
        f"stream={stream} last_user=\"{last_user_hint}\""
    )


def _safe_get(d: Any, *keys: str) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


# ── Anthropic SSE parser ─────────────────────────────────────────────────────

def _parse_anthropic_sse(event_lines: list[str]) -> dict:
    """
    Anthropic splits usage across two events:
      * message_start.message.usage      → input_tokens (+ initial cache fields)
      * message_delta.usage              → cumulative output_tokens
    We merge into one usage dict so downstream cost code sees the same shape
    as non-streaming response.body.usage. The shape already matches the rate
    card; no normalization needed.
    """
    text_blocks: dict[int, list[str]] = {}
    tool_uses: dict[int, dict] = {}
    stop_reason = None
    usage: dict[str, int] = {}

    for line in event_lines:
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue

        t = ev.get("type")
        if t == "message_start":
            msg = ev.get("message", {})
            u   = msg.get("usage") or {}
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                if k in u:
                    usage[k] = u[k]
        elif t == "content_block_start":
            idx = ev.get("index", 0)
            block = ev.get("content_block", {})
            if block.get("type") == "tool_use":
                tool_uses[idx] = {"name": block.get("name"), "id": block.get("id"), "input_chunks": []}
        elif t == "content_block_delta":
            idx = ev.get("index", 0)
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta":
                text_blocks.setdefault(idx, []).append(delta.get("text", ""))
            elif delta.get("type") == "input_json_delta":
                if idx in tool_uses:
                    tool_uses[idx]["input_chunks"].append(delta.get("partial_json", ""))
        elif t == "message_delta":
            delta = ev.get("delta", {})
            if "stop_reason" in delta:
                stop_reason = delta["stop_reason"]
            u = ev.get("usage") or {}
            for k in ("output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                if k in u:
                    usage[k] = u[k]

    return {
        "text_blocks": ["".join(parts) for parts in text_blocks.values()],
        "tool_uses": [
            {"name": tu["name"], "id": tu.get("id"), "input": "".join(tu["input_chunks"])}
            for tu in tool_uses.values()
        ],
        "stop_reason": stop_reason,
        "usage": usage or None,
    }


# Kept for backwards-compat with existing tests that import this name.
_summarise_response_event = _parse_anthropic_sse


# ── OpenAI-compatible (Moonshot/Kimi) ────────────────────────────────────────

def _normalize_openai_usage(u: dict | None) -> dict | None:
    """
    OpenAI / Moonshot shape:
        {prompt_tokens, completion_tokens, total_tokens,
         prompt_cache_hit_tokens?, prompt_cache_miss_tokens?}

    Moonshot's `prompt_tokens` is the FULL prompt (cached + uncached).
    Rate card expects `input_tokens` = NON-cached portion only,
    `cache_read_input_tokens` = cached portion. Subtract the cache hits.
    """
    if not isinstance(u, dict):
        return None
    full_prompt = int(u.get("prompt_tokens") or 0)
    cache_hit   = int(u.get("prompt_cache_hit_tokens") or 0)
    out = {
        "input_tokens":             max(0, full_prompt - cache_hit),
        "output_tokens":            int(u.get("completion_tokens") or 0),
        "cache_read_input_tokens":  cache_hit,
    }
    # OpenAI-style providers don't bill separately for cache writes, so omit.
    return out


def _parse_openai_sse(event_lines: list[str]) -> dict:
    """
    OpenAI-style streaming: each `data:` line is a JSON `chat.completion.chunk`.
    Text arrives in `choices[].delta.content`. The final chunk usually carries
    `usage` (Moonshot includes prompt_cache_hit_tokens here). Tool calls
    arrive as `choices[].delta.tool_calls` with partial `function.arguments`.
    """
    text_parts: list[str] = []
    tool_uses: dict[int, dict] = {}  # by tool_call index
    stop_reason = None
    usage_raw: dict | None = None

    for line in event_lines:
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue

        u = ev.get("usage")
        if isinstance(u, dict):
            usage_raw = u

        for ch in ev.get("choices") or []:
            delta = ch.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                fn  = tc.get("function") or {}
                if idx not in tool_uses:
                    tool_uses[idx] = {"id": tc.get("id"), "name": fn.get("name"), "input_chunks": []}
                if fn.get("name") and not tool_uses[idx]["name"]:
                    tool_uses[idx]["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    tool_uses[idx]["input_chunks"].append(fn["arguments"])
            if ch.get("finish_reason"):
                stop_reason = ch["finish_reason"]

    return {
        "text_blocks": ["".join(text_parts)] if text_parts else [],
        "tool_uses": [
            {"name": tu["name"], "id": tu.get("id"), "input": "".join(tu["input_chunks"])}
            for tu in tool_uses.values()
        ],
        "stop_reason": stop_reason,
        "usage": _normalize_openai_usage(usage_raw),
    }


def _summarise_openai_nonstream(resp_json: dict) -> dict:
    """Same shape as _parse_openai_sse, but from a non-streaming response."""
    if not isinstance(resp_json, dict):
        return {"text_blocks": [], "tool_uses": [], "stop_reason": None, "usage": None}
    text_blocks: list[str] = []
    tool_uses: list[dict] = []
    stop_reason = None
    for ch in resp_json.get("choices") or []:
        msg = ch.get("message") or {}
        if isinstance(msg.get("content"), str):
            text_blocks.append(msg["content"])
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_uses.append({"name": fn.get("name"), "id": tc.get("id"), "input": fn.get("arguments")})
        if ch.get("finish_reason"):
            stop_reason = ch["finish_reason"]
    return {
        "text_blocks": text_blocks,
        "tool_uses":   tool_uses,
        "stop_reason": stop_reason,
        "usage":       _normalize_openai_usage(resp_json.get("usage")),
    }


# ── Google (Gemini) ──────────────────────────────────────────────────────────

def _normalize_google_usage(u: dict | None) -> dict | None:
    """
    Google shape: {promptTokenCount, candidatesTokenCount, totalTokenCount,
                   cachedContentTokenCount?}
    promptTokenCount is FULL prompt (cached + uncached). Same logic as
    Moonshot — subtract cached to get the chargeable input.
    """
    if not isinstance(u, dict):
        return None
    full_prompt = int(u.get("promptTokenCount") or 0)
    cached      = int(u.get("cachedContentTokenCount") or 0)
    return {
        "input_tokens":             max(0, full_prompt - cached),
        "output_tokens":            int(u.get("candidatesTokenCount") or 0),
        "cache_read_input_tokens":  cached,
    }


def _parse_google_sse(event_lines: list[str]) -> dict:
    """
    Google streaming (`?alt=sse`): each `data:` line is a full GenerateContentResponse.
    Text arrives in candidates[0].content.parts[].text. usageMetadata appears
    in the last chunk (and may appear in earlier chunks too — we take the
    latest seen).
    """
    text_parts: list[str] = []
    tool_uses_acc: list[dict] = []
    stop_reason = None
    usage_raw: dict | None = None

    for line in event_lines:
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue

        for cand in ev.get("candidates") or []:
            content = cand.get("content") or {}
            for part in content.get("parts") or []:
                if isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                fc = part.get("functionCall")
                if isinstance(fc, dict):
                    tool_uses_acc.append({
                        "name":  fc.get("name"),
                        "id":    None,
                        "input": json.dumps(fc.get("args", {})),
                    })
            if cand.get("finishReason"):
                stop_reason = cand["finishReason"]

        if isinstance(ev.get("usageMetadata"), dict):
            usage_raw = ev["usageMetadata"]

    return {
        "text_blocks": ["".join(text_parts)] if text_parts else [],
        "tool_uses":   tool_uses_acc,
        "stop_reason": stop_reason,
        "usage":       _normalize_google_usage(usage_raw),
    }


def _summarise_google_nonstream(resp_json: dict) -> dict:
    if not isinstance(resp_json, dict):
        return {"text_blocks": [], "tool_uses": [], "stop_reason": None, "usage": None}
    text_blocks: list[str] = []
    tool_uses:   list[dict] = []
    stop_reason = None
    for cand in resp_json.get("candidates") or []:
        content = cand.get("content") or {}
        text_blocks.extend(
            p.get("text", "") for p in content.get("parts") or []
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
        for p in content.get("parts") or []:
            fc = p.get("functionCall") if isinstance(p, dict) else None
            if isinstance(fc, dict):
                tool_uses.append({"name": fc.get("name"), "id": None, "input": json.dumps(fc.get("args", {}))})
        if cand.get("finishReason"):
            stop_reason = cand["finishReason"]
    return {
        "text_blocks": text_blocks,
        "tool_uses":   tool_uses,
        "stop_reason": stop_reason,
        "usage":       _normalize_google_usage(resp_json.get("usageMetadata")),
    }


# ── Slot classification ──────────────────────────────────────────────────────
# Tag every call with which Forge slot it belongs to. This is the key unlock
# for the QA matrix: "same prompt across N model strategies → per-slot cost
# breakdown." Without slot tags, we know the session cost a total of $X but
# can't say which $ went to design vs build vs fixer vs chat — and that's
# the whole comparison.
#
# Identification is heuristic, in priority order:
#   1. Subagent system-prompt fingerprints (most reliable — opencode's
#      subagent definitions ship hard-coded phrases we control).
#   2. Tool-list shape — design-analyst and design-critic are read-only;
#      a call with no write/edit/bash and the analyst phrase is design.
#   3. Default to `build` (main coder loop) when no fingerprint matches.

_SLOT_FINGERPRINTS: tuple[tuple[str, str], ...] = (
    # (lowercased substring to find in system prompt, slot label)
    ("you are the design analyst", "design"),
    ("you are the design critic",  "design_review"),
    ("you are the error-fixer",    "fixer"),
)


def _flatten_system(system: Any) -> str:
    """opencode passes `system` as either a string, a list of text blocks
    (Anthropic), or a `systemInstruction` dict (Google). Concatenate to one
    lowercased string for fingerprint matching."""
    if isinstance(system, str):
        return system.lower()
    if isinstance(system, list):
        parts: list[str] = []
        for b in system:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
        return " ".join(parts).lower()
    if isinstance(system, dict):
        out: list[str] = []
        for p in system.get("parts") or []:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                out.append(p["text"])
        return " ".join(out).lower()
    return ""


def _classify_slot(body: Any) -> str:
    """
    Return the Forge slot for this call. Reads `body.system` + `body.tools`
    only — both are already preserved in the log record, so this is a pure
    string operation, no extra payload.

    Returns one of: "design", "design_review", "fixer", "chat", "build".
    """
    if not isinstance(body, dict):
        return "build"

    sys_text = _flatten_system(body.get("system") or body.get("systemInstruction"))
    for needle, slot in _SLOT_FINGERPRINTS:
        if needle in sys_text:
            return slot

    # Tool-shape heuristic: a call with no write capability is probably chat
    # (UI clarification, color tweak, "what does this do") rather than a real
    # build turn. Anthropic shape: tools = [{name, ...}]. OpenAI-compat shape:
    # tools = [{type: "function", function: {name, ...}}]. Google: tools = [{...}].
    tools = body.get("tools") or []
    if isinstance(tools, list):
        names: set[str] = set()
        for t in tools:
            if not isinstance(t, dict):
                continue
            n = t.get("name") or _safe_get(t, "function", "name")
            if isinstance(n, str):
                names.add(n.lower())
        writeable = {"write", "edit", "bash", "patch", "create_file", "shell"}
        if names and not (names & writeable):
            return "chat"

    return "build"


# ── Cost computation (uses forge-qa rate card) ───────────────────────────────

def _compute_cost(model: str | None, usage: dict | None) -> dict | None:
    """
    Returns {input_usd, output_usd, cache_read_usd, cache_write_usd, total_usd}
    or None if cost can't be computed. The proxy must NEVER fail because of
    cost computation — log the call regardless.
    """
    if _rate_card is None or not model or not isinstance(usage, dict):
        return None
    try:
        b = _rate_card.cost_from_usage(model, usage)
        return b.as_dict()
    except KeyError:
        log.warning(f"no rate card entry for model={model!r} — cost not computed")
        return None
    except Exception as exc:
        log.warning(f"cost computation failed for model={model!r}: {exc}")
        return None


def _summarise_response_one_line(summary: dict, cost: dict | None = None) -> str:
    n_text  = len(summary["text_blocks"])
    text_chars = sum(len(t) for t in summary["text_blocks"])
    tool_names = [t.get("name") for t in summary["tool_uses"]]
    u = summary.get("usage") or {}
    tok = f"in={u.get('input_tokens',0)} out={u.get('output_tokens',0)}"
    cs  = f" cost=${cost['total_usd']:.4f}" if cost else ""
    return (
        f"text_blocks={n_text}({text_chars}c) "
        f"tool_uses={tool_names or '[]'} "
        f"stop={summary['stop_reason']} {tok}{cs}"
    )


def _scrub_secrets(headers: dict) -> dict:
    """Hide bearer/api keys in logs — we shouldn't be writing keys to disk."""
    out = dict(headers)
    for k in list(out.keys()):
        kl = k.lower()
        if kl in ("x-api-key", "authorization", "x-goog-api-key"):
            v = out[k]
            out[k] = f"{v[:12]}…{v[-4:]}" if isinstance(v, str) and len(v) > 20 else "<hidden>"
    return out


def _write_call_file(call_id: str, record: dict) -> Path:
    """Persist one full call (request + response) as a forensic JSON file."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = CALLS_DIR / f"{ts}_{call_id}.json"
    path.write_text(json.dumps(record, indent=2, default=str))
    return path


def _append_jsonl(record: dict) -> None:
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ── Generic vendor handler ───────────────────────────────────────────────────

async def _handle_vendor_call(
    *,
    vendor:           str,
    request:          Request,
    upstream_url:     str,
    upstream_params:  dict | None,
    extract_model:    Callable[[dict | None, Request], str | None],
    parse_sse:        Callable[[list[str]], dict],
    parse_nonstream:  Callable[[dict], dict],
    log_path_label:   str,
) -> Response:
    """
    The shared streaming/non-streaming forwarder. Vendor-specific bits arrive
    as callbacks. Records every call to the JSONL + per-call file with cost.
    """
    call_id = uuid.uuid4().hex[:12]
    t0      = time.monotonic()

    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes) if body_bytes else None
    except json.JSONDecodeError:
        body_json = None

    headers_in = dict(request.headers)
    is_stream  = _detect_stream(vendor, body_json, request)

    req_summary = _summarise_request(body_json) if isinstance(body_json, dict) else f"<non-json body: {len(body_bytes)} bytes>"
    log.info(f"[{call_id}] → {vendor} {request.method} {log_path_label}  {req_summary}")

    upstream_headers = {
        k: v for k, v in headers_in.items()
        if k.lower() not in ("host", "content-length", "connection", "accept-encoding")
    }
    # See proxy_messages docstring — never let upstream gzip the SSE.
    upstream_headers["accept-encoding"] = "identity"

    model = extract_model(body_json, request)

    try:
        if is_stream:
            req = _client.build_request(
                "POST",
                upstream_url,
                headers=upstream_headers,
                content=body_bytes,
                params=upstream_params,
            )
            r = await _client.send(req, stream=True)
            collected: list[str] = []

            async def stream_and_log() -> AsyncIterator[bytes]:
                # Watchdog: any single gap > _IDLE_TOKEN_TIMEOUT_S between
                # upstream chunks → treat the turn as stalled, close upstream,
                # emit a vendor-agnostic terminator so the SDK parser unblocks,
                # and log the kill. Resets on every byte received, so a slow-
                # but-alive stream continues normally. See module-level note
                # on _IDLE_TOKEN_TIMEOUT_S for the cost/latency tradeoff.
                stalled = False
                aiter   = r.aiter_raw().__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            aiter.__anext__(), timeout=_IDLE_TOKEN_TIMEOUT_S
                        )
                    except asyncio.TimeoutError:
                        stalled = True
                        break
                    except StopAsyncIteration:
                        break
                    collected.append(chunk.decode("utf-8", errors="replace"))
                    yield chunk

                # Best-effort upstream close; never let a close error mask
                # the real outcome we're about to log.
                try:
                    await r.aclose()
                except Exception:
                    pass

                elapsed_ms = int((time.monotonic() - t0) * 1000)
                full_text  = "".join(collected)

                if stalled:
                    # Emit a vendor-agnostic SSE terminator. OpenAI-compatible
                    # clients (Moonshot) require `data: [DONE]` to consider
                    # the stream finished; Anthropic/Google clients tolerate
                    # a foreign terminator and treat it as EOF. Without this
                    # the client may sit on a half-open stream waiting for
                    # its own internal timeout — defeating the whole point
                    # of the watchdog. Clients also have read timeouts that
                    # will fire eventually; this is belt-and-braces.
                    yield b"data: [DONE]\n\n"
                    log.warning(
                        f"[{call_id}] ⚠ stalled — no chunk in {_IDLE_TOKEN_TIMEOUT_S:.0f}s, "
                        f"aborted upstream after {elapsed_ms}ms, {len(full_text)}b buffered"
                    )
                    resp_summary = {
                        "text_blocks": [],
                        "tool_uses":   [],
                        "stop_reason": "stalled",
                        "usage":       None,
                    }
                    # 504 in the persisted record signals "we never got a
                    # complete response" — distinct from a real upstream 5xx,
                    # which would carry r.status_code from a non-streaming
                    # failure. Cost stays None (we don't know the usage).
                    _persist_record(call_id, elapsed_ms, True, vendor, model, None,
                                    request.method, log_path_label, headers_in, body_json,
                                    504, dict(r.headers), resp_summary, full_text, None)
                    return

                event_lines  = full_text.split("\n")
                resp_summary = parse_sse(event_lines)
                cost         = _compute_cost(model, resp_summary.get("usage"))
                log.info(
                    f"[{call_id}] ← {r.status_code} {_summarise_response_one_line(resp_summary, cost)}  "
                    f"({elapsed_ms}ms, {len(full_text)}b)"
                )
                _persist_record(call_id, elapsed_ms, True, vendor, model, cost,
                                request.method, log_path_label, headers_in, body_json,
                                r.status_code, dict(r.headers), resp_summary, full_text, None)

            return StreamingResponse(
                stream_and_log(),
                status_code=r.status_code,
                headers={
                    k: v for k, v in r.headers.items()
                    if k.lower() not in ("content-length", "transfer-encoding", "connection")
                },
                media_type=r.headers.get("content-type", "text/event-stream"),
            )

        # Non-streaming
        r = await _client.post(
            upstream_url,
            headers=upstream_headers,
            content=body_bytes,
            params=upstream_params,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        try:
            resp_json = r.json()
        except Exception:
            resp_json = None

        resp_summary = parse_nonstream(resp_json) if isinstance(resp_json, dict) else \
            {"text_blocks": [], "tool_uses": [], "stop_reason": None, "usage": None}
        cost = _compute_cost(model, resp_summary.get("usage"))
        log.info(
            f"[{call_id}] ← {r.status_code} {_summarise_response_one_line(resp_summary, cost)}  "
            f"({elapsed_ms}ms)"
        )
        _persist_record(call_id, elapsed_ms, False, vendor, model, cost,
                        request.method, log_path_label, headers_in, body_json,
                        r.status_code, dict(r.headers), resp_summary, None, resp_json)
        return JSONResponse(content=resp_json, status_code=r.status_code)

    except httpx.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.error(f"[{call_id}] ✗ {vendor} proxy error ({elapsed_ms}ms): {exc}")
        return JSONResponse(
            content={"type": "error", "error": {"type": "proxy_error", "message": str(exc)}},
            status_code=502,
        )


def _detect_stream(vendor: str, body_json: dict | None, request: Request) -> bool:
    """
    Anthropic + OpenAI-compatible: `stream: true` in the JSON body.
    Google: the endpoint path is `:streamGenerateContent`, OR `?alt=sse` query.
    """
    if isinstance(body_json, dict) and body_json.get("stream"):
        return True
    if vendor == "google":
        if ":streamGenerateContent" in request.url.path:
            return True
        if request.query_params.get("alt") == "sse":
            return True
    return False


def _persist_record(
    call_id: str, elapsed_ms: int, stream: bool, vendor: str,
    model: str | None, cost: dict | None,
    method: str, path: str, req_headers: dict, body_json: Any,
    status: int, resp_headers: dict, summary: dict,
    raw_sse: str | None, resp_body: Any,
) -> None:
    slot = _classify_slot(body_json)
    record = {
        "call_id":    call_id,
        "ts":         _now_iso(),
        "elapsed_ms": elapsed_ms,
        "stream":     stream,
        "vendor":     vendor,
        "model":      model,
        "slot":       slot,    # design | design_review | fixer | chat | build
        "cost":       cost,
        "request": {
            "method":  method,
            "path":    path,
            "headers": _scrub_secrets(req_headers),
            "body":    body_json,
        },
        "response": {
            "status":  status,
            "headers": resp_headers,
            "summary": summary,
            **({"raw_sse": _truncate(raw_sse)} if raw_sse is not None else {}),
            **({"body": resp_body} if resp_body is not None else {}),
        },
    }
    _append_jsonl(record)
    _write_call_file(call_id, record)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "anthropic_upstream": ANTHROPIC_UPSTREAM,
        "moonshot_upstream":  MOONSHOT_UPSTREAM,
        "google_upstream":    GOOGLE_UPSTREAM,
        "rate_card_loaded":   _rate_card is not None,
    }


# ── Anthropic /v1/messages ───────────────────────────────────────────────────

@app.post("/v1/messages")
async def proxy_anthropic_messages(request: Request):
    return await _handle_vendor_call(
        vendor          = "anthropic",
        request         = request,
        upstream_url    = f"{ANTHROPIC_UPSTREAM.rstrip('/')}/v1/messages",
        upstream_params = None,
        extract_model   = lambda body, _req: body.get("model") if isinstance(body, dict) else None,
        parse_sse       = _parse_anthropic_sse,
        parse_nonstream = _summarise_anthropic_nonstream,
        log_path_label  = "/v1/messages",
    )


def _summarise_anthropic_nonstream(resp_json: dict) -> dict:
    """Non-streaming Anthropic response → same shape as the SSE parser."""
    text_blocks: list[str] = []
    tool_uses:   list[dict] = []
    for block in resp_json.get("content") or []:
        if block.get("type") == "text":
            text_blocks.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_uses.append({"name": block.get("name"), "id": block.get("id"), "input": block.get("input")})
    return {
        "text_blocks": text_blocks,
        "tool_uses":   tool_uses,
        "stop_reason": resp_json.get("stop_reason"),
        "usage":       resp_json.get("usage"),
    }


# ── Moonshot /moonshot/v1/chat/completions ───────────────────────────────────

@app.post("/moonshot/v1/chat/completions")
async def proxy_moonshot_chat(request: Request):
    return await _handle_vendor_call(
        vendor          = "moonshot",
        request         = request,
        upstream_url    = f"{MOONSHOT_UPSTREAM.rstrip('/')}/v1/chat/completions",
        upstream_params = None,
        extract_model   = lambda body, _req: body.get("model") if isinstance(body, dict) else None,
        parse_sse       = _parse_openai_sse,
        parse_nonstream = _summarise_openai_nonstream,
        log_path_label  = "/moonshot/v1/chat/completions",
    )


# ── Google /google/v1beta/models/{model}:(stream)GenerateContent ─────────────

_GOOGLE_MODEL_RE = re.compile(r"^v1beta/models/([^:]+):(\w+)$")


@app.post("/google/v1beta/models/{model_action:path}")
async def proxy_google_generate_content(model_action: str, request: Request):
    """
    `model_action` is e.g. "gemini-3.5-flash:generateContent" or
    "gemini-3.5-flash:streamGenerateContent" — the @ai-sdk/google package
    builds these URLs from the model id.
    """
    upstream_url = f"{GOOGLE_UPSTREAM.rstrip('/')}/v1beta/models/{model_action}"
    # Pass query params through (alt=sse, key=..., etc.)
    qp = dict(request.query_params)

    def _extract_model(_body: Any, _req: Request) -> str | None:
        # Model is in the URL path, before the colon.
        if ":" in model_action:
            return model_action.split(":", 1)[0]
        return None

    return await _handle_vendor_call(
        vendor          = "google",
        request         = request,
        upstream_url    = upstream_url,
        upstream_params = qp or None,
        extract_model   = _extract_model,
        parse_sse       = _parse_google_sse,
        parse_nonstream = _summarise_google_nonstream,
        log_path_label  = f"/google/v1beta/models/{model_action}",
    )


# ── Pass-through catch-alls (for endpoints we don't deep-log) ────────────────

async def _passthrough(
    upstream_base: str,
    sub_path: str,
    request: Request,
    vendor_label: str,
) -> Response:
    call_id = uuid.uuid4().hex[:8]
    t0      = time.monotonic()
    body_bytes = await request.body()
    upstream_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "connection", "accept-encoding")
    }
    upstream_url = f"{upstream_base.rstrip('/')}/{sub_path}"
    try:
        r = await _client.request(
            request.method,
            upstream_url,
            headers=upstream_headers,
            content=body_bytes,
            params=dict(request.query_params),
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info(f"[{call_id}] {vendor_label} {request.method} /{sub_path} → {r.status_code} ({elapsed_ms}ms)")
        return Response(
            content=r.content,
            status_code=r.status_code,
            headers={k: v for k, v in r.headers.items() if k.lower() not in ("content-length", "transfer-encoding", "connection")},
            media_type=r.headers.get("content-type"),
        )
    except httpx.HTTPError as exc:
        log.error(f"[{call_id}] ✗ {vendor_label} passthrough error: {exc}")
        return JSONResponse(
            content={"type": "error", "error": {"type": "proxy_error", "message": str(exc)}},
            status_code=502,
        )


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_anthropic_other(path: str, request: Request):
    return await _passthrough(ANTHROPIC_UPSTREAM, f"v1/{path}", request, "anthropic")


@app.api_route("/moonshot/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_moonshot_other(path: str, request: Request):
    return await _passthrough(MOONSHOT_UPSTREAM, path, request, "moonshot")


@app.api_route("/google/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_google_other(path: str, request: Request):
    return await _passthrough(GOOGLE_UPSTREAM, path, request, "google")


if __name__ == "__main__":
    import uvicorn
    log.info(f"forge-llm-proxy starting on :{PORT}")
    log.info(f"  anthropic upstream: {ANTHROPIC_UPSTREAM}")
    log.info(f"  moonshot  upstream: {MOONSHOT_UPSTREAM}")
    log.info(f"  google    upstream: {GOOGLE_UPSTREAM}")
    log.info(f"  rate card loaded:   {_rate_card is not None}")
    log.info(f"  JSONL log:          {LOG_FILE}")
    log.info(f"  Per-call:           {CALLS_DIR}/")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
