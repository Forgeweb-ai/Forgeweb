"""
forge/model/ai.py
=================
Direct AI provider backend.

This backend keeps Forge's existing generation/update/chat flow, but swaps the
wire client underneath it. DeepSeek, OpenAI, Gemini's OpenAI-compatible API, and
custom OpenAI-compatible endpoints use /chat/completions. Anthropic uses its
native Messages API and is adapted to the same response shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

import httpx

from forge.config import config
from forge.model.base import INTENT_SYSTEM_PROMPT
from forge.model.codegen import CodegenBackend


@dataclass
class _Message:
    content: str


@dataclass
class _Delta:
    content: str | None = None
    reasoning: str | None = None
    reasoning_content: str | None = None


@dataclass
class _Choice:
    message: _Message | None = None
    delta: _Delta | None = None


@dataclass
class _Response:
    choices: list[_Choice]


class _ChatCompletions:
    def __init__(self, owner: "_ProviderClient"):
        self._owner = owner

    def create(self, **kwargs):
        return self._owner.create_chat_completion(**kwargs)


class _Chat:
    def __init__(self, owner: "_ProviderClient"):
        self.completions = _ChatCompletions(owner)


class _ProviderClient:
    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str,
        thinking: str,
        timeout: httpx.Timeout,
    ):
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.thinking = thinking
        self.timeout = timeout
        self.chat = _Chat(self)

    def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        stream: bool = False,
        **_: object,
    ):
        if self.provider == "anthropic":
            return self._anthropic_completion(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=stream,
            )
        return self._openai_compatible_completion(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
        )

    def _openai_compatible_completion(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        stream: bool,
    ):
        url = f"{self.base_url}/chat/completions"

        # Kimi-K2 / moonshot extended-thinking models require temperature=1.
        # Any other value returns 400 Bad Request (same constraint as OpenAI o3/o3-mini).
        _is_kimi_thinking = self.provider == "kimi" and (
            "kimi-k2" in model.lower() or "kimi-k1.5" in model.lower()
        )
        effective_temperature = 1.0 if _is_kimi_thinking else temperature

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": effective_temperature,
            "stream": stream,
        }
        # Add thinking param for providers/models that support it
        if self.thinking and self.thinking != "disabled":
            if self.provider in {"deepseek", "grok"}:
                payload["thinking"] = {"type": self.thinking}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if stream:
            return self._stream_openai_compatible(url, headers, payload)

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        message = data["choices"][0]["message"]
        content = message.get("content") or message.get("reasoning_content") or ""
        return _Response(choices=[_Choice(message=_Message(content=content))])

    def _stream_openai_compatible(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict,
    ) -> Iterable[_Response]:
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line.removeprefix("data:").strip()
                    if raw == "[DONE]":
                        break
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        yield _Response(choices=[])
                        continue
                    delta_obj = choices[0].get("delta") or {}
                    yield _Response(
                        choices=[
                            _Choice(
                                delta=_Delta(
                                    content=delta_obj.get("content"),
                                    reasoning=delta_obj.get("reasoning"),
                                    reasoning_content=delta_obj.get("reasoning_content"),
                                )
                            )
                        ]
                    )

    def _anthropic_completion(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        stream: bool,
    ):
        system, user_messages = self._split_anthropic_messages(messages)
        url = f"{self.base_url}/messages"
        payload = {
            "model": model,
            "messages": user_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        if stream:
            return self._stream_anthropic(url, headers, payload)

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        content = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        return _Response(choices=[_Choice(message=_Message(content=content))])

    def _stream_anthropic(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict,
    ) -> Iterable[_Response]:
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line.removeprefix("data:").strip()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "content_block_delta":
                        delta = data.get("delta") or {}
                        text = delta.get("text")
                        if text:
                            yield _Response(choices=[_Choice(delta=_Delta(content=text))])

    @staticmethod
    def _split_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        converted: list[dict] = []

        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")

            # ── Multipart content (vision messages with image_url blocks) ─────
            if isinstance(content, list):
                # Convert OpenAI image_url format → Anthropic image source format
                anthropic_blocks: list[dict] = []
                for block in content:
                    btype = block.get("type", "")
                    if btype == "image_url":
                        url = block.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            # data:<mime>;base64,<data>
                            header, _, b64data = url.partition(",")
                            media_type = header.removeprefix("data:").split(";")[0]
                            anthropic_blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64data,
                                },
                            })
                    elif btype == "text":
                        text = block.get("text", "")
                        if text.strip():
                            anthropic_blocks.append({"type": "text", "text": text})

                if not anthropic_blocks:
                    continue

                if role == "system":
                    # Extract text for system prompt (images ignored here)
                    system_text = " ".join(
                        b["text"] for b in anthropic_blocks if b.get("type") == "text"
                    )
                    if system_text.strip():
                        system_parts.append(system_text)
                    continue

                if role not in {"user", "assistant"}:
                    role = "user"

                if converted and converted[-1]["role"] == role:
                    prev = converted[-1]["content"]
                    if isinstance(prev, list):
                        converted[-1]["content"] = prev + anthropic_blocks
                    else:
                        converted[-1]["content"] = [{"type": "text", "text": prev}] + anthropic_blocks
                else:
                    converted.append({"role": role, "content": anthropic_blocks})
                continue

            # ── Plain string content (original path) ─────────────────────────
            if not str(content).strip():
                continue
            if role == "system":
                system_parts.append(str(content))
                continue
            if role not in {"user", "assistant"}:
                role = "user"

            if converted and converted[-1]["role"] == role:
                prev = converted[-1]["content"]
                if isinstance(prev, list):
                    # Append as a text block to existing multipart message
                    converted[-1]["content"].append({"type": "text", "text": str(content)})
                else:
                    converted[-1]["content"] += "\n\n" + str(content)
            else:
                converted.append({"role": role, "content": str(content)})

        if converted and converted[0]["role"] == "assistant":
            converted.insert(0, {"role": "user", "content": "Continue."})

        return "\n\n".join(system_parts), converted


class AIBackend(CodegenBackend):
    # Default base URLs, text/coding models, and vision model overrides.
    # AI_MODEL and AI_BASE_URL in .env always take priority.
    # "vision_model" is the model to use when an image is attached to the request.
    # If a provider has a single unified model that handles both, omit vision_model.
    DEFAULTS = {
        # ── Kimi native (moonshot.ai) — faster + cheaper than Together ──────
        "kimi": {
            "base_url": "https://api.moonshot.ai/v1",
            "model": "kimi-k2-0711-preview",
        },
        "moonshot": {
            "base_url": "https://api.moonshot.ai/v1",
            "model": "kimi-k2-0711-preview",
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            # DeepSeek has no vision model on the public API — image calls will
            # use the same model (it will just ignore the image).
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1",
            # gpt-4.1 already handles vision — no separate model needed.
        },
        "chatgpt": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1",
        },
        "gemini": {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-2.5-pro",
            # gemini-2.5-pro is already multimodal.
        },
        "anthropic": {
            "base_url": "https://api.anthropic.com/v1",
            "model": "claude-sonnet-4-6",
            # claude-sonnet-4-6 is already multimodal.
        },
        "claude": {
            "base_url": "https://api.anthropic.com/v1",
            "model": "claude-sonnet-4-6",
        },
        "grok": {
            "base_url": "https://api.x.ai/v1",
            "model": "grok-4",
        },
        "xai": {
            "base_url": "https://api.x.ai/v1",
            "model": "grok-4",
        },
        "together": {
            "base_url": "https://api.together.xyz/v1",
            "model": "moonshotai/Kimi-K2-Instruct",
        },
        # ── Qwen (Alibaba / DashScope — OpenAI-compatible endpoint) ──────────
        # Best coding model:  qwen2.5-coder-32b-instruct  (strong at code, cheap)
        # Best vision model:  qwen-vl-max                 (highest accuracy on images)
        # Other good options: qwen2.5-72b-instruct (general), qwen-max (frontier)
        # Vision models:      qwen-vl-plus, qwen-vl-max, qwen2-vl-72b-instruct
        # Non-vision models:  qwen2.5-coder-32b-instruct, qwen2.5-72b-instruct,
        #                     qwen-plus, qwen-max, qwen-turbo
        "qwen": {
            "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "model": "qwen2.5-coder-32b-instruct",   # best for coding
            "vision_model": "qwen3-vl-flash",          # auto-used when image is attached
        },
        "custom": {
            "base_url": "",
            "model": "",
        },
    }

    # Per-provider env-var names for API keys.
    # Prefer the provider-specific key so switching AI_PROVIDER does not
    # accidentally send a key from the previous provider. AI_API_KEY remains a
    # generic fallback for custom/proxy setups.
    _KEY_ENVS: dict[str, str] = {
        "kimi":      "MOONSHOT_API_KEY",
        "moonshot":  "MOONSHOT_API_KEY",
        "deepseek":  "DEEPSEEK_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "chatgpt":   "OPENAI_API_KEY",
        "gemini":    "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "claude":    "ANTHROPIC_API_KEY",
        "grok":      "XAI_API_KEY",
        "xai":       "XAI_API_KEY",
        "together":  "TOGETHER_API_KEY",
        "qwen":      "QWEN_API_KEY",
    }

    def __init__(self, provider: str | None = None):
        import os
        import httpx

        provider_name = (provider or config.model.ai_provider or "kimi").lower()
        # Normalise aliases to a canonical name for routing
        _aliases = {"chatgpt": "openai", "claude": "anthropic", "xai": "grok", "moonshot": "kimi"}
        canonical = _aliases.get(provider_name, provider_name)

        defaults = self.DEFAULTS.get(provider_name, self.DEFAULTS["custom"])

        self.provider = canonical  # used for routing (openai/anthropic/grok/…)
        self.base_url = config.model.ai_base_url or defaults["base_url"]
        self.model = config.model.ai_model or defaults["model"]

        # Vision model: AI_VISION_MODEL env var > provider default > same as model.
        # Used automatically when generate_codebase() receives an image_base64 arg.
        self.vision_model = (
            config.model.ai_vision_model
            or defaults.get("vision_model", "")
            or self.model
        )

        # Key resolution: provider-specific env var beats generic AI_API_KEY.
        specific_key_var = self._KEY_ENVS.get(provider_name, "")
        self.api_key = (
            (os.getenv(specific_key_var, "") if specific_key_var else "")
            or config.model.ai_api_key
        )

        if not self.base_url:
            raise ValueError("AI_BASE_URL is required when AI_PROVIDER=custom")
        if not self.model:
            raise ValueError("AI_MODEL is required when AI_PROVIDER=custom")

        timeout = httpx.Timeout(connect=20.0, read=90.0, write=20.0, pool=20.0)
        self.client = _ProviderClient(
            provider=canonical,  # normalised: anthropic/openai/grok/deepseek/gemini/qwen/…
            api_key=self.api_key,
            base_url=self.base_url,
            thinking=config.model.ai_thinking,
            timeout=timeout,
        )
        print(
            f"[ai] client init  provider={self.provider}  base_url={self.base_url}"
            f"  model={self.model}"
            + (f"  vision_model={self.vision_model}" if self.vision_model != self.model else ""),
            flush=True,
        )

        # ── Tiered task clients ───────────────────────────────────────────────
        # Build per-task (client, model) pairs from optional env var overrides.
        # Falls back to (self.client, self.model) when no override is configured.
        self._task_clients: dict[str, tuple["_ProviderClient", str]] = {
            "intent":  self._build_task_client(config.model.intent_model),
            "chat":    self._build_task_client(config.model.chat_model),
            "plan":    self._build_task_client(config.model.plan_model),
            "codegen": self._build_task_client(config.model.codegen_model),
            "context": self._build_task_client(config.model.context_model),
        }
        # Log task routing summary (only non-default entries)
        for task, (tc, tm) in self._task_clients.items():
            if tm != self.model:
                prov = tc.provider if tc is not self.client else self.provider
                print(f"[ai] task routing  {task:8s} → {prov}/{tm}", flush=True)

    # ── Task-client resolver ──────────────────────────────────────────────────

    def _build_task_client(self, spec: str) -> tuple["_ProviderClient", str]:
        """
        Parse a "provider/model-name" spec and return (client, model_str).

        Rules:
          - Empty spec          → (self.client, self.model)   [main model, no change]
          - Same provider       → (self.client, model_name)   [reuse connection]
          - Different provider  → new _ProviderClient          [separate API key/URL]

        Note: Together AI model slugs contain slashes (e.g. moonshotai/Kimi-K2-Instruct).
        The convention here is that the FIRST path segment is the provider name,
        and everything after the first slash is the model identifier.
        """
        import os
        import httpx as _httpx

        spec = (spec or "").strip()
        if not spec:
            return self.client, self.model

        slash = spec.find("/")
        if slash < 0:
            # No slash — treat whole string as a model name for the same provider.
            return self.client, spec

        provider_raw = spec[:slash].lower()
        model_name   = spec[slash + 1:]

        _aliases = {"chatgpt": "openai", "claude": "anthropic", "xai": "grok"}
        canonical = _aliases.get(provider_raw, provider_raw)

        if canonical == self.provider:
            # Same provider — reuse existing client, just change the model string.
            return self.client, model_name

        # Different provider — build a dedicated lightweight client.
        defaults = self.DEFAULTS.get(provider_raw, self.DEFAULTS["custom"])
        base_url = defaults["base_url"]

        key_var = self._KEY_ENVS.get(provider_raw, "")
        api_key = (os.getenv(key_var, "") if key_var else "") or config.model.ai_api_key

        # Fast timeout for cheap tasks (intent / chat / context).
        # 30s read is plenty; these calls are much smaller than full codegen.
        timeout = _httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        client = _ProviderClient(
            provider=canonical,
            api_key=api_key,
            base_url=base_url,
            thinking="disabled",   # no extended thinking for fast task models
            timeout=timeout,
        )
        return client, model_name

    # ── Intent classification (uses INTENT_MODEL) ─────────────────────────────

    async def classify_intent(
        self,
        message: str,
        has_project: bool,
        project_name: str = "",
    ) -> str:
        """
        Returns 'chat', 'build', or 'update'.
        Routed to the cheapest/fastest model (INTENT_MODEL env var).
        """
        import asyncio as _asyncio

        intent_client, intent_model = self._task_clients["intent"]

        context_line = (
            f"Existing project: {project_name}" if has_project
            else "No existing project yet."
        )
        all_messages = [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user",   "content": f"{context_line}\n\nUser message: {message}"},
        ]

        loop  = _asyncio.get_event_loop()
        queue: _asyncio.Queue = _asyncio.Queue()

        def _run_sync():
            try:
                resp = intent_client.chat.completions.create(
                    model       = intent_model,
                    messages    = all_messages,
                    max_tokens  = 5,
                    temperature = 0.0,
                    stream      = False,
                )
                token = (resp.choices[0].message.content or "").strip().lower()
                loop.call_soon_threadsafe(queue.put_nowait, token)
            except Exception as e:
                print(f"[ai] intent classify error ({e}) — defaulting to context guess", flush=True)
                loop.call_soon_threadsafe(queue.put_nowait, "update" if has_project else "build")

        loop.run_in_executor(None, _run_sync)
        raw = await queue.get()

        if "build"  in raw: return "build"
        if "update" in raw: return "update"
        return "chat"

    # ── Chat (uses CHAT_MODEL) ────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        codebase_context: list[dict] | None = None,
        system_override: str | None = None,
        runtime_context: str | None = None,
    ):
        """Conversational replies routed to CHAT_MODEL."""
        chat_client, chat_model = self._task_clients["chat"]
        orig_client, orig_model = self.client, self.model
        self.client, self.model = chat_client, chat_model
        try:
            async for token in super().chat(
                messages, codebase_context, system_override, runtime_context
            ):
                yield token
        finally:
            self.client, self.model = orig_client, orig_model

    # ── Context summary (uses CONTEXT_MODEL) ─────────────────────────────────

    async def generate_context_summary(self, files: list[dict]) -> str:
        """Project context summarisation routed to CONTEXT_MODEL."""
        ctx_client, ctx_model = self._task_clients["context"]
        orig_client, orig_model = self.client, self.model
        self.client, self.model = ctx_client, ctx_model
        try:
            return await super().generate_context_summary(files)
        finally:
            self.client, self.model = orig_client, orig_model

    async def generate_codebase(
        self,
        prompt: str,
        language: str = "auto",
        extra_context: str = "",
        stack: dict | None = None,
        image_base64: str | None = None,
        image_type: str | None = None,
    ):
        """
        Wrap the parent generator to auto-swap to the vision model when an image
        is present and the vision model differs from the default coding model.
        The swap is scoped to this call — self.model is restored on exit.
        """
        original_model = self.model
        if image_base64 and self.vision_model and self.vision_model != self.model:
            self.model = self.vision_model
            print(
                f"[ai] image attached → switching model: {original_model} → {self.vision_model}",
                flush=True,
            )
        try:
            async for chunk in super().generate_codebase(
                prompt, language, extra_context, stack, image_base64, image_type
            ):
                yield chunk
        finally:
            self.model = original_model

    async def health(self) -> dict:
        return {
            "backend": "ai",
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "status": "ok" if self.api_key else "missing_api_key",
        }

    @staticmethod
    def _humanize_error(e: Exception, model: str) -> str:
        msg = str(e)
        klass = type(e).__name__
        return (
            f"{klass}: {msg}\n"
            f"Hint: check AI_PROVIDER, AI_MODEL, AI_BASE_URL, and AI_API_KEY in forge/.env, "
            f"then restart the backend. Current model: `{model}`."
        )
