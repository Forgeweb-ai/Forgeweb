"""
Verifies _classify_slot against real opencode subagent system prompts.

The fingerprints are pulled directly from forge-opencode-config/opencode.json
— if those subagent prompts change, this test will fail loudly and we update
both in lockstep.

Run:
    python3 forge-qa/tests/test_slot_classify.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "forge-llm-proxy"))
sys.path.insert(0, str(ROOT / "forge-qa" / "src"))
os.environ["FORGE_QA_PATH"] = str(ROOT / "forge-qa")

import proxy  # noqa: E402

# ── Realistic system prompt openings (excerpted from opencode.json) ──────────

DESIGN_ANALYST_SYS = (
    "You are the design analyst. Your only job is to pick a design profile "
    "from /forge-skills/design-pool/profiles/ and emit a structured design spec."
)

DESIGN_CRITIC_SYS = (
    "You are the design critic. You receive the design spec from design-analyst "
    "and the generated code from the main agent."
)

ERROR_FIXER_SYS = (
    "You are the error-fixer subagent. Your job is to clear the runtime-error "
    "queue for this project by fixing the root causes."
)

MAIN_AGENT_SYS = (
    "You are OpenCode, the best coding agent on the planet.\n\n"
    "You are an interactive CLI tool that helps users with software engineering tasks."
)


def _body(system, tools=None):
    return {"model": "claude-sonnet-4-6", "system": system, "tools": tools or []}


# ── Fingerprint matching ─────────────────────────────────────────────────────

def test_design_analyst_string_system() -> None:
    assert proxy._classify_slot(_body(DESIGN_ANALYST_SYS)) == "design"
    print("OK  string system → design")


def test_design_analyst_anthropic_block_system() -> None:
    # opencode sends Anthropic's system as a list of {type:text,text:...} blocks
    blocks = [{"type": "text", "text": DESIGN_ANALYST_SYS, "cache_control": {"type": "ephemeral"}}]
    assert proxy._classify_slot(_body(blocks)) == "design"
    print("OK  anthropic block-array system → design")


def test_design_critic() -> None:
    assert proxy._classify_slot(_body(DESIGN_CRITIC_SYS)) == "design_review"
    print("OK  design critic → design_review")


def test_error_fixer() -> None:
    body = _body(ERROR_FIXER_SYS, tools=[
        {"name": "read"}, {"name": "edit"}, {"name": "bash"},
    ])
    assert proxy._classify_slot(body) == "fixer"
    print("OK  error-fixer → fixer (even with write tools)")


def test_main_agent_defaults_to_build() -> None:
    # Main agent system prompt has no subagent fingerprint AND has write tools.
    body = _body(MAIN_AGENT_SYS, tools=[
        {"name": "bash"}, {"name": "edit"}, {"name": "write"}, {"name": "read"},
    ])
    assert proxy._classify_slot(body) == "build"
    print("OK  main agent (write tools) → build")


def test_chat_heuristic_no_write_tools() -> None:
    # Read-only tools and no subagent fingerprint → chat (likely a clarifying
    # question or one-off lookup).
    body = _body("You are a helpful assistant.", tools=[
        {"name": "read"}, {"name": "glob"}, {"name": "grep"},
    ])
    assert proxy._classify_slot(body) == "chat"
    print("OK  read-only tools → chat")


def test_chat_heuristic_no_tools_at_all() -> None:
    body = _body("Just chat with me.", tools=[])
    # Empty tools list → falls through heuristic → default build
    assert proxy._classify_slot(body) == "build"
    print("OK  empty tools → build (default, no override)")


# ── Cross-vendor system shapes ───────────────────────────────────────────────

def test_google_system_instruction_shape() -> None:
    """Google passes systemInstruction = {parts: [{text: ...}], role: ...}."""
    body = {
        "model": "gemini-3.5-flash",
        "systemInstruction": {
            "role": "system",
            "parts": [{"text": DESIGN_ANALYST_SYS}],
        },
        "tools": [],
    }
    assert proxy._classify_slot(body) == "design"
    print("OK  google systemInstruction shape → design")


def test_openai_function_tools_shape() -> None:
    """OpenAI/Moonshot tools = [{type:function, function:{name:...}}]."""
    body = {
        "model": "kimi-k2.6",
        "system": "You are a helpful assistant.",
        "tools": [
            {"type": "function", "function": {"name": "read"}},
            {"type": "function", "function": {"name": "grep"}},
        ],
    }
    assert proxy._classify_slot(body) == "chat"
    print("OK  openai function-tools shape → chat (read-only)")


# ── Persisted record carries the slot ────────────────────────────────────────

def test_persist_record_writes_slot(tmp_dir: Path | None = None) -> None:
    """End-to-end: feed a known body through _persist_record and confirm the
    slot lands in the JSON on disk."""
    import json
    import shutil

    test_dir = ROOT / "forge-qa" / "results" / "_test_slot_persist"
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir(parents=True, exist_ok=True)

    # Redirect the proxy's globals to our temp dir so we don't pollute real logs.
    orig_log_file = proxy.LOG_FILE
    orig_calls    = proxy.CALLS_DIR
    proxy.LOG_FILE  = test_dir / "x.jsonl"
    proxy.CALLS_DIR = test_dir / "calls"
    proxy.CALLS_DIR.mkdir(exist_ok=True)
    try:
        proxy._persist_record(
            call_id="testcall", elapsed_ms=42, stream=False, vendor="anthropic",
            model="claude-sonnet-4-6", cost={"total_usd": 0.0},
            method="POST", path="/v1/messages",
            req_headers={"x-session-affinity": "ses_test"},
            body_json={"model": "claude-sonnet-4-6", "system": DESIGN_ANALYST_SYS, "tools": []},
            status=200, resp_headers={}, summary={"text_blocks": [], "tool_uses": [], "stop_reason": None, "usage": None},
            raw_sse=None, resp_body={},
        )
        files = list((test_dir / "calls").iterdir())
        assert len(files) == 1, files
        rec = json.loads(files[0].read_text())
        assert rec["slot"] == "design", rec
        print(f"OK  persisted record carries slot=design")
    finally:
        proxy.LOG_FILE  = orig_log_file
        proxy.CALLS_DIR = orig_calls
        shutil.rmtree(test_dir, ignore_errors=True)


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
