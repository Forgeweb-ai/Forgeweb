"""
forge/forge/agent/loop.py
==========================
The AI Agent run loop.

run_agent() takes a task + project context and runs an LLM tool-use loop:
  1. Send system prompt + task to the LLM (with tool schemas)
  2. Yield AgentEvent objects as they arrive (thinking, tool_call, tool_result, etc.)
  3. When the LLM calls a tool → dispatch it → send result back as tool_result message
  4. Loop until the LLM stops calling tools (stop_reason = end_turn) or emits "DONE"
  5. Yield a final "done" event with summary

Designed to work with Anthropic's native Messages API (tool_use blocks).
Falls back gracefully to a JSON-parsing approach for OpenAI-compatible providers.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import AsyncIterator, Any

from forge.config import config
from forge.runner.workspace import workspace_manager
from forge.agent.tools import TOOL_SCHEMAS, dispatch_tool
from forge.agent.prompts import get_agent_system_prompt

# ─────────────────────────────────────────────────────────────────────────────
# AgentEvent type (mirrors forge-ui/lib/agentApi.ts)
# ─────────────────────────────────────────────────────────────────────────────

AgentEvent = dict[str, Any]

# Maximum number of tool-call iterations to prevent infinite loops
MAX_ITERATIONS = 40

# ─────────────────────────────────────────────────────────────────────────────
# Per-project abort registry (project_id → bool)
# ─────────────────────────────────────────────────────────────────────────────
_abort_flags: dict[str, bool] = {}


def abort_agent(project_id: str) -> None:
    """Signal the agent loop for this project to stop after the current iteration."""
    _abort_flags[project_id] = True


def _clear_abort(project_id: str) -> None:
    _abort_flags.pop(project_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

async def run_agent(
    project_id: str,
    task: str,
    stack: dict | None = None,
) -> AsyncIterator[AgentEvent]:
    """
    Run the AI agent for a project task.

    Yields AgentEvent dicts:
      {"type": "thinking",        "text": "..."}
      {"type": "tool_call",       "tool": "exec_command", "params": {...}}
      {"type": "tool_result",     "tool": "exec_command", "output": "...", "exit_code": 0}
      {"type": "file_change",     "path": "src/App.tsx", "op": "write"}
      {"type": "process_started", "name": "frontend", "port": 5173, "pid": 12345}
      {"type": "process_stopped", "name": "frontend"}
      {"type": "status",          "text": "Installing dependencies…"}
      {"type": "done",            "summary": "..."}
      {"type": "error",           "text": "..."}
    """
    _clear_abort(project_id)

    # Ensure workspace exists
    workspace_manager.create(project_id, stack=stack)

    # Get allocated ports for this project
    ports = workspace_manager.load_config(project_id).get("ports", {})
    fe_port = ports.get("fe", 5173)
    be_port = ports.get("be", 8001)

    system_prompt = get_agent_system_prompt(stack, fe_port=fe_port, be_port=be_port)

    # Build the provider client (same logic as AIBackend)
    client, model, provider = _make_client()

    # ── Design Director pre-pass ─────────────────────────────────────────────
    # Fire a fast LLM call to generate a unique visual identity brief for this
    # specific project. This ensures every app has its own look — palette,
    # fonts, personality — derived from the actual prompt rather than a template.
    # The brief is injected into the task before the main agent loop runs.
    yield {"type": "status", "text": "Designing visual identity…"}
    design_brief = await _run_design_director(task, client, model, provider)
    if design_brief:
        task = f"{task}\n\n---\n## Visual Design Brief (follow exactly)\n\n{design_brief}"
        yield {"type": "thinking", "text": f"Design brief ready:\n{design_brief[:400]}…"}

    if provider == "anthropic":
        async for event in _run_anthropic_loop(
            project_id=project_id,
            task=task,
            system_prompt=system_prompt,
            client=client,
            model=model,
        ):
            if _abort_flags.get(project_id):
                yield {"type": "status", "text": "Agent stopped by user."}
                yield {"type": "done", "summary": "Stopped by user."}
                return
            yield event
    else:
        async for event in _run_openai_loop(
            project_id=project_id,
            task=task,
            system_prompt=system_prompt,
            client=client,
            model=model,
            provider=provider,
        ):
            if _abort_flags.get(project_id):
                yield {"type": "status", "text": "Agent stopped by user."}
                yield {"type": "done", "summary": "Stopped by user."}
                return
            yield event


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic tool-use loop
# ─────────────────────────────────────────────────────────────────────────────

async def _run_anthropic_loop(
    project_id: str,
    task: str,
    system_prompt: str,
    client: Any,
    model: str,
) -> AsyncIterator[AgentEvent]:
    """Tool-use loop using Anthropic's native Messages API format."""
    import httpx

    messages: list[dict] = [{"role": "user", "content": task}]
    api_key  = client.api_key
    base_url = client.base_url.rstrip("/")
    headers  = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    # Convert tool schemas from OpenAI-style to Anthropic style
    anthropic_tools = [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["input_schema"],
        }
        for t in TOOL_SCHEMAS
    ]

    for iteration in range(MAX_ITERATIONS):
        yield {"type": "status", "text": f"Thinking… (step {iteration + 1})"}

        payload = {
            "model": model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": messages,
            "tools": anthropic_tools,
        }

        # Non-streaming call (simpler to handle tool_use blocks)
        try:
            loop = asyncio.get_running_loop()
            resp_data = await loop.run_in_executor(
                None,
                lambda: _post_json(f"{base_url}/messages", headers, payload),
            )
        except Exception as e:
            yield {"type": "error", "text": f"API error: {e}"}
            return

        stop_reason = resp_data.get("stop_reason", "")
        content_blocks: list[dict] = resp_data.get("content", [])

        # Emit thinking/text blocks
        for block in content_blocks:
            if block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    yield {"type": "thinking", "text": text}

        # Append assistant message
        messages.append({"role": "assistant", "content": content_blocks})

        # If no tool calls → done
        tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]
        if not tool_use_blocks or stop_reason == "end_turn":
            # Extract final summary from last text block
            summary = next(
                (b.get("text", "") for b in reversed(content_blocks) if b.get("type") == "text"),
                "Task completed.",
            )
            yield {"type": "done", "summary": summary}
            return

        # Execute each tool call
        tool_results: list[dict] = []
        for block in tool_use_blocks:
            tool_name  = block.get("name", "")
            tool_input = block.get("input", {})
            tool_id    = block.get("id", "")

            yield {"type": "tool_call", "tool": tool_name, "params": tool_input}

            # Run the tool
            result = await dispatch_tool(project_id, tool_name, tool_input)

            # Emit specialised events for certain tools
            async for derived in _derive_events(tool_name, tool_input, result):
                yield derived

            output_str = json.dumps(result, ensure_ascii=False)
            exit_code  = result.get("exit_code")
            yield {
                "type":      "tool_result",
                "tool":      tool_name,
                "output":    output_str[:4000],
                "exit_code": exit_code,
            }

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": tool_id,
                "content":     output_str[:8000],
            })

        # Send tool results back to the model
        messages.append({"role": "user", "content": tool_results})

    yield {"type": "error", "text": f"Agent exceeded {MAX_ITERATIONS} iterations without finishing."}


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI-compatible tool-use loop (DeepSeek, OpenAI, Gemini, etc.)
# ─────────────────────────────────────────────────────────────────────────────

async def _run_openai_loop(
    project_id: str,
    task: str,
    system_prompt: str,
    client: Any,
    model: str,
    provider: str,
) -> AsyncIterator[AgentEvent]:
    """
    Tool-use loop for OpenAI-compatible providers.
    Uses the function_calling / tools format.
    """
    import httpx

    # Convert tool schemas to OpenAI function-calling format
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOL_SCHEMAS
    ]

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": task},
    ]

    api_key  = client.api_key
    base_url = client.base_url.rstrip("/")
    headers  = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for iteration in range(MAX_ITERATIONS):
        yield {"type": "status", "text": f"Thinking… (step {iteration + 1})"}

        payload = {
            "model": model,
            "messages": messages,
            "tools": openai_tools,
            "tool_choice": "auto",
            "max_tokens": 4096,
        }

        try:
            loop = asyncio.get_running_loop()
            resp_data = await loop.run_in_executor(
                None,
                lambda: _post_json(f"{base_url}/chat/completions", headers, payload),
            )
        except Exception as e:
            yield {"type": "error", "text": f"API error: {e}"}
            return

        choice     = (resp_data.get("choices") or [{}])[0]
        msg        = choice.get("message", {})
        finish     = choice.get("finish_reason", "")
        tool_calls = msg.get("tool_calls") or []
        text       = (msg.get("content") or "").strip()

        if text:
            yield {"type": "thinking", "text": text}

        messages.append(msg)  # append assistant message as-is

        if not tool_calls or finish == "stop":
            yield {"type": "done", "summary": text or "Task completed."}
            return

        for tc in tool_calls:
            fn         = tc.get("function", {})
            tool_name  = fn.get("name", "")
            tc_id      = tc.get("id", "")
            try:
                tool_input = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                tool_input = {}

            yield {"type": "tool_call", "tool": tool_name, "params": tool_input}

            result = await dispatch_tool(project_id, tool_name, tool_input)

            async for derived in _derive_events(tool_name, tool_input, result):
                yield derived

            output_str = json.dumps(result, ensure_ascii=False)
            exit_code  = result.get("exit_code")
            yield {
                "type":      "tool_result",
                "tool":      tool_name,
                "output":    output_str[:4000],
                "exit_code": exit_code,
            }

            messages.append({
                "role":         "tool",
                "tool_call_id": tc_id,
                "content":      output_str[:8000],
            })

    yield {"type": "error", "text": f"Agent exceeded {MAX_ITERATIONS} iterations without finishing."}


# ─────────────────────────────────────────────────────────────────────────────
# Derived events — richer UI signals from specific tool calls
# ─────────────────────────────────────────────────────────────────────────────

async def _derive_events(
    tool_name: str,
    tool_input: dict,
    result: dict,
) -> AsyncIterator[AgentEvent]:
    """
    Emit higher-level UI events from specific tool results.
    E.g. write_file → file_change, start_process → process_started
    """
    if tool_name == "write_file" and result.get("ok"):
        yield {
            "type": "file_change",
            "path": tool_input.get("path", ""),
            "op":   "write",
        }

    elif tool_name == "stop_process" and result.get("ok"):
        yield {
            "type": "process_stopped",
            "name": tool_input.get("name", ""),
        }

    elif tool_name == "start_process" and result.get("ok"):
        yield {
            "type": "process_started",
            "name": tool_input.get("name", ""),
            "port": tool_input.get("port", 0),
            "pid":  result.get("pid"),
        }

    elif tool_name == "exec_command":
        # Emit a status blurb summarising the command
        cmd = tool_input.get("command", "")[:60]
        yield {"type": "status", "text": f"$ {cmd}"}

    elif tool_name == "search_code":
        pattern = tool_input.get("pattern", "")[:40]
        scope   = tool_input.get("path", ".")
        matches = result.get("match_count", 0)
        yield {"type": "status", "text": f"🔍 /{pattern}/ in {scope} → {matches} match{'es' if matches != 1 else ''}"}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _run_design_director(task: str, client: Any, model: str, provider: str) -> str:
    """
    Fire a single fast LLM call that acts as a "Design Director".
    Returns a concise design brief (palette, fonts, personality, sections)
    that is unique to this specific project prompt.

    The brief is injected into the agent task so it builds to a specific
    visual identity rather than defaulting to a generic dark template.
    """
    DESIGN_PROMPT = """You are a senior brand designer and creative director.
Given a webapp description, produce a concise VISUAL DESIGN BRIEF in markdown.
Be specific and opinionated — make every decision intentional and unique to this product.

Output ONLY the brief in this exact format (no preamble, no explanation):

## Brand Personality
[2-3 adjectives, e.g. "bold, energetic, youthful" or "minimal, trustworthy, clinical"]

## Colour Palette
- Background: [hex] — [why]
- Surface/cards: [hex]
- Primary text: [hex]
- Muted text: [hex]
- Brand accent: [hex] — [why this colour suits this product]
- Accent glow: [rgba for shadows/glows]
- Border: [hex]

## Typography
- Display/headings: [Google Font name] — [weight range, e.g. 400/700]
- Body: [Google Font name] — [weight range]
- Vibe: [e.g. "editorial serif headings contrast with clean sans body"]

## Animation Style
[One sentence — e.g. "Smooth GSAP scroll-triggered fade-ups with slight Y translation, subtle parallax on hero"]

## Page Sections (in order)
1. [Section name] — [one-line description]
2. [Section name] — [one-line description]
[etc — list all sections the page needs]

## Hero Headline
"[Punchy 5-10 word headline specific to this product]"

## Product Name
[Invented brand name that fits — not a generic placeholder]
"""

    user_msg = f"Webapp description: {task[:800]}"

    try:
        loop = asyncio.get_running_loop()
        if provider == "anthropic":
            api_key  = client.api_key
            base_url = client.base_url.rstrip("/")
            headers  = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "max_tokens": 1024,
                "system": DESIGN_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            }
            resp = await loop.run_in_executor(
                None,
                lambda: _post_json(f"{base_url}/messages", headers, payload),
            )
            for block in resp.get("content", []):
                if block.get("type") == "text":
                    return block.get("text", "").strip()
        else:
            # OpenAI-compatible (kimi, deepseek, together, etc.)
            api_key  = client.api_key
            base_url = client.base_url.rstrip("/")
            headers  = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": DESIGN_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
            }
            resp = await loop.run_in_executor(
                None,
                lambda: _post_json(f"{base_url}/chat/completions", headers, payload),
            )
            choice = (resp.get("choices") or [{}])[0]
            return (choice.get("message", {}).get("content") or "").strip()
    except Exception as e:
        print(f"[design-director] failed (non-fatal): {e}", flush=True)
        return ""  # fail silently — main agent runs without the brief

    return ""


def _make_client() -> tuple[Any, str, str]:
    """
    Build a lightweight client config dict (api_key, base_url) + model + provider.
    Reuses the same config.model settings as AIBackend to stay consistent.
    """
    import os

    provider_name = (config.model.ai_provider or "kimi").lower()
    _aliases      = {
        "chatgpt":  "openai",
        "claude":   "anthropic",
        "xai":      "grok",
        "moonshot": "kimi",   # moonshot.ai = kimi native
    }
    canonical = _aliases.get(provider_name, provider_name)

    DEFAULTS = {
        # ── Kimi native (moonshot.ai) — faster + cheaper than Together ──────
        "kimi":      {"base_url": "https://api.moonshot.ai/v1",                              "model": "kimi-k2-0711-preview"},
        "deepseek":  {"base_url": "https://api.deepseek.com/v1",                             "model": "deepseek-chat"},
        "openai":    {"base_url": "https://api.openai.com/v1",                               "model": "gpt-4.1"},
        "gemini":    {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-pro"},
        "anthropic": {"base_url": "https://api.anthropic.com/v1",                           "model": "claude-sonnet-4-6"},
        "grok":      {"base_url": "https://api.x.ai/v1",                                    "model": "grok-4"},
        "together":  {"base_url": "https://api.together.xyz/v1",                            "model": "moonshotai/Kimi-K2-Instruct"},
    }
    KEY_ENVS = {
        "kimi":      "MOONSHOT_API_KEY",
        "deepseek":  "DEEPSEEK_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "gemini":    "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "grok":      "XAI_API_KEY",
        "together":  "TOGETHER_API_KEY",
    }

    # Hardcoded Kimi native key as fallback (set via env to override)
    _HARDCODED_KEYS: dict[str, str] = {}  # keys come from .env MOONSHOT_API_KEY only

    defs     = DEFAULTS.get(canonical, {"base_url": "", "model": ""})
    base_url = config.model.ai_base_url  or defs["base_url"]
    model    = config.model.ai_model     or defs["model"]
    key_env  = KEY_ENVS.get(canonical, "")
    # Provider-specific key wins over generic AI_API_KEY so that e.g. MOONSHOT_API_KEY
    # is used for kimi even when AI_API_KEY is set to a different provider's key.
    api_key  = (
        (os.getenv(key_env, "") if key_env else "")
        or config.model.ai_api_key
        or _HARDCODED_KEYS.get(canonical, "")
    )

    class _Client:
        pass

    c = _Client()
    c.api_key  = api_key   # type: ignore[attr-defined]
    c.base_url = base_url  # type: ignore[attr-defined]
    return c, model, canonical


def _post_json(url: str, headers: dict, payload: dict) -> dict:
    """Blocking HTTP POST with JSON body. Used inside run_in_executor."""
    import httpx
    with httpx.Client(timeout=httpx.Timeout(connect=20, read=120, write=30, pool=10)) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()
