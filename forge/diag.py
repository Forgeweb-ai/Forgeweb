"""
forge/diag.py
=============
End-to-end connectivity probe for the configured Forge model backend.

Runs four checks:

  1. Raw HTTP non-streaming      -- proves key + model + network work
  2. Raw HTTP streaming          -- proves SSE chunks actually arrive
  3. Production client non-stream -- proves Forge's backend client is wired up
  4. Production client streaming  -- matches the production streaming path

Run from forge/:
    python3 diag.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()


DEFAULTS = {
    "kimi": {
        "base_url": "https://api.moonshot.ai/v1",
        "model": "kimi-k2-0711-preview",
        "key_envs": ["MOONSHOT_API_KEY", "AI_API_KEY"],
        "kind": "openai",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.ai/v1",
        "model": "kimi-k2-0711-preview",
        "key_envs": ["MOONSHOT_API_KEY", "AI_API_KEY"],
        "kind": "openai",
    },
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "model": "grok-4",
        "key_envs": ["XAI_API_KEY", "AI_API_KEY"],
        "kind": "openai",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "model": "grok-4",
        "key_envs": ["XAI_API_KEY", "AI_API_KEY"],
        "kind": "openai",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_envs": ["DEEPSEEK_API_KEY", "AI_API_KEY"],
        "kind": "openai",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1",
        "key_envs": ["OPENAI_API_KEY", "AI_API_KEY"],
        "kind": "openai",
    },
    "chatgpt": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1",
        "key_envs": ["OPENAI_API_KEY", "AI_API_KEY"],
        "kind": "openai",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-pro",
        "key_envs": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "AI_API_KEY"],
        "kind": "openai",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-6",
        "key_envs": ["ANTHROPIC_API_KEY", "AI_API_KEY"],
        "kind": "anthropic",
    },
    "claude": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-6",
        "key_envs": ["ANTHROPIC_API_KEY", "AI_API_KEY"],
        "kind": "anthropic",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "model": "moonshotai/Kimi-K2-Instruct",
        "key_envs": ["TOGETHER_API_KEY", "AI_API_KEY"],
        "kind": "openai",
    },
    "qwen": {
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-coder-plus",
        "key_envs": ["QWEN_API_KEY", "AI_API_KEY"],
        "kind": "openai",
    },
    "custom": {
        "base_url": "",
        "model": "",
        "key_envs": ["AI_API_KEY"],
        "kind": "openai",
    },
}

# Aliases → canonical provider name (for routing + display)
_ALIASES: dict[str, str] = {
    "chatgpt":  "openai",
    "claude":   "anthropic",
    "xai":      "grok",
    "moonshot": "kimi",
}


def mask_key(key: str) -> str:
    if not key:
        return "EMPTY"
    if len(key) <= 10:
        return f"{key[:2]}...{key[-2:]} ({len(key)} chars)"
    return f"{key[:7]}...{key[-4:]} ({len(key)} chars)"


def first_env(names: list[str]) -> tuple[str, str]:
    for name in names:
        value = os.getenv(name, "")
        if value:
            return name, value
    return names[0], ""


def selected_config() -> dict[str, str]:
    backend = os.getenv("MODEL_BACKEND", "together").lower()

    if backend == "together":
        provider_raw = "together"
        defaults = DEFAULTS[provider_raw]
        key_name, key = first_env(defaults["key_envs"])
        return {
            "backend": backend,
            "provider": provider_raw,
            "provider_raw": provider_raw,
            "kind": defaults["kind"],
            "key_name": key_name,
            "key": key,
            "model": os.getenv("TOGETHER_MODEL", defaults["model"]),
            "base_url": os.getenv("TOGETHER_BASE_URL", defaults["base_url"]).rstrip("/"),
            "thinking": "disabled",
        }

    # MODEL_BACKEND=ai (or anything else) → read AI_PROVIDER
    provider_raw = os.getenv("AI_PROVIDER", "deepseek").lower()
    canonical = _ALIASES.get(provider_raw, provider_raw)  # xai→grok, claude→anthropic, etc.

    defaults = DEFAULTS.get(provider_raw, DEFAULTS["custom"])
    key_name, key = first_env(defaults["key_envs"])

    return {
        "backend": backend,
        "provider": canonical,      # normalised name shown in output
        "provider_raw": provider_raw,
        "kind": defaults["kind"],
        "key_name": key_name,
        "key": key,
        "model": os.getenv("AI_MODEL", defaults["model"]),
        "base_url": os.getenv("AI_BASE_URL", defaults["base_url"]).rstrip("/"),
        "thinking": os.getenv("AI_THINKING", "disabled"),
    }


CFG = selected_config()

print("=" * 68)
print(f"backend : {CFG['backend']}")
print(f"provider: {CFG['provider']}", end="")
if CFG["provider_raw"] != CFG["provider"]:
    print(f"  (alias: {CFG['provider_raw']})", end="")
print()
print(f"key env : {CFG['key_name']}")
print(f"key     : {mask_key(CFG['key'])}")
print(f"model   : {CFG['model'] or 'MISSING'}")
print(f"base    : {CFG['base_url'] or 'MISSING'}")
thinking = CFG.get("thinking", "disabled")
if thinking and thinking != "disabled":
    print(f"thinking: {thinking}")
print("=" * 68)

if not CFG["key"]:
    print(f"ERR: {CFG['key_name']} is empty. Put it in forge/.env")
    sys.exit(1)
if not CFG["model"]:
    print("ERR: model is empty. Set AI_MODEL or TOGETHER_MODEL in forge/.env")
    sys.exit(1)
if not CFG["base_url"]:
    print("ERR: base URL is empty. Set AI_BASE_URL in forge/.env")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERR: install requests with: pip install requests")
    sys.exit(1)


def print_error_help(status_code: int, body: str) -> None:
    print(f"    body: {body[:900]}")
    message = ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, list) and parsed:
            parsed = parsed[0]
        if isinstance(parsed, dict):
            error = parsed.get("error") or {}
            if isinstance(error, dict):
                message = error.get("message", "")
    except json.JSONDecodeError:
        pass
    if "you passed" in message:
        print(f"    sent model: {CFG['model']!r}")
    print("\nCommon causes:")
    print("  400 -> request shape or unsupported model for this endpoint")
    print("  401 -> API key invalid, expired, or for a different provider")
    print("  402 -> out of credits / billing required")
    print("  404 -> wrong base URL or model name")
    print("  429 -> rate limited")
    print("  5xx -> provider outage or model temporarily unavailable")


def raw_non_stream() -> str:
    print("\n[1] Raw HTTP, stream=False")
    started = time.time()

    if CFG["kind"] == "anthropic":
        url = f"{CFG['base_url']}/messages"
        headers = {
            "x-api-key": CFG["key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": CFG["model"],
            "messages": [{"role": "user", "content": "Say hi in 5 words."}],
            "max_tokens": 40,
            "stream": False,
        }
        response = requests.post(url, headers=headers, json=body, timeout=60)
        print(f"    HTTP {response.status_code} in {time.time() - started:.1f}s")
        if response.status_code != 200:
            print_error_help(response.status_code, response.text)
            sys.exit(2)
        data = response.json()
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
    else:
        url = f"{CFG['base_url']}/chat/completions"
        headers = {
            "Authorization": f"Bearer {CFG['key']}",
            "Content-Type": "application/json",
        }
        body = {
            "model": CFG["model"],
            "messages": [{"role": "user", "content": "Say hi in 5 words."}],
            "max_tokens": 40,
            "stream": False,
        }
        thinking = CFG.get("thinking", "disabled")
        if thinking and thinking != "disabled" and CFG["provider"] in {"deepseek", "grok"}:
            body["thinking"] = {"type": thinking}
        response = requests.post(url, headers=headers, json=body, timeout=60)
        print(f"    HTTP {response.status_code} in {time.time() - started:.1f}s")
        if response.status_code != 200:
            print_error_help(response.status_code, response.text)
            sys.exit(2)
        data = response.json()
        message = data["choices"][0]["message"]
        text = message.get("content") or message.get("reasoning_content") or ""

    print(f"    reply: {text[:160]!r}")
    return text


def raw_stream() -> str:
    print("\n[2] Raw HTTP, stream=True  (SSE)")
    started = time.time()
    chunks = 0
    text = ""

    if CFG["kind"] == "anthropic":
        url = f"{CFG['base_url']}/messages"
        headers = {
            "x-api-key": CFG["key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": CFG["model"],
            "messages": [{"role": "user", "content": "Say hi in 5 words."}],
            "max_tokens": 40,
            "stream": True,
        }
        with requests.post(url, headers=headers, json=body, stream=True, timeout=60) as response:
            print(f"    HTTP {response.status_code}")
            if response.status_code != 200:
                print_error_help(response.status_code, response.text)
                sys.exit(2)
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line.removeprefix("data:").strip())
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "content_block_delta":
                    continue
                token = (obj.get("delta") or {}).get("text")
                if token:
                    chunks += 1
                    text += token
                    if chunks <= 3:
                        print(f"    chunk {chunks}: {token!r}")
    else:
        url = f"{CFG['base_url']}/chat/completions"
        headers = {
            "Authorization": f"Bearer {CFG['key']}",
            "Content-Type": "application/json",
        }
        body = {
            "model": CFG["model"],
            "messages": [{"role": "user", "content": "Say hi in 5 words."}],
            "max_tokens": 40,
            "stream": True,
        }
        thinking = CFG.get("thinking", "disabled")
        if thinking and thinking != "disabled" and CFG["provider"] in {"deepseek", "grok"}:
            body["thinking"] = {"type": thinking}
        with requests.post(url, headers=headers, json=body, stream=True, timeout=60) as response:
            print(f"    HTTP {response.status_code}")
            if response.status_code != 200:
                print_error_help(response.status_code, response.text)
                sys.exit(2)
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                payload = line.removeprefix("data:").strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                token = (
                    delta.get("content")
                    or delta.get("reasoning")
                    or delta.get("reasoning_content")
                )
                if token:
                    chunks += 1
                    text += token
                    if chunks <= 3:
                        print(f"    chunk {chunks}: {token!r}")

    print(f"    OK: {chunks} chunks in {time.time() - started:.1f}s -> {text[:160]!r}")
    if chunks == 0:
        print("\nERR: streaming connected but returned 0 text chunks.")
        sys.exit(3)
    return text


def production_non_stream() -> str:
    print("\n[3] Production client, stream=False")
    from forge.model import get_backend

    backend = get_backend()
    started = time.time()
    # Kimi-K2 / moonshot thinking models require temperature=1 (like OpenAI o3).
    _is_kimi = getattr(backend, "provider", "") == "kimi"
    _temp = 1.0 if _is_kimi else 0.0
    response = backend.client.chat.completions.create(
        model=backend.model,
        messages=[{"role": "user", "content": "Say hi in 5 words."}],
        max_tokens=40,
        temperature=_temp,
        stream=False,
    )
    text = response.choices[0].message.content or ""
    print(f"    OK: {time.time() - started:.1f}s -> {text[:160]!r}")
    return text


def production_stream() -> str:
    print("\n[4] Production client, stream=True")
    from forge.model import get_backend

    backend = get_backend()
    started = time.time()
    _is_kimi = getattr(backend, "provider", "") == "kimi"
    _temp = 1.0 if _is_kimi else 0.0
    stream = backend.client.chat.completions.create(
        model=backend.model,
        messages=[{"role": "user", "content": "Say hi in 5 words."}],
        max_tokens=40,
        temperature=_temp,
        stream=True,
    )

    chunks = 0
    text = ""
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        token = (
            getattr(delta, "content", None)
            or getattr(delta, "reasoning", None)
            or getattr(delta, "reasoning_content", None)
        )
        if token:
            chunks += 1
            text += token
            if chunks <= 3:
                print(f"    chunk {chunks}: {token!r}")

    print(f"    OK: {chunks} chunks in {time.time() - started:.1f}s -> {text[:160]!r}")
    if chunks == 0:
        print("\nERR: production streaming returned 0 text chunks.")
        sys.exit(3)
    return text


try:
    raw_non_stream()
    raw_stream()
    production_non_stream()
    production_stream()
except requests.RequestException as exc:
    print(f"\nERR: network/provider request failed: {type(exc).__name__}: {exc}")
    sys.exit(2)
except Exception as exc:
    print(f"\nERR: diagnostic failed: {type(exc).__name__}: {exc}")
    sys.exit(4)

print("\n" + "=" * 68)
print("ALL GREEN -- configured provider connectivity and streaming work.")
print("Restart FastAPI so the app picks up the same .env values.")
print("=" * 68)
