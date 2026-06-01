"""
forge/model/local.py
====================
Local llama.cpp backend — runs your fine-tuned Phi-3.5 Mini GGUF on-device.
No internet. Zero API cost. This is the production mobile backend.

Install: pip install llama-cpp-python
Model:   ./models/forge-phi3.5-Q4_K_M.gguf  (your fine-tuned export)
"""

import json
from typing import AsyncIterator
from forge.model.base import (
    ModelBackend,
    CODEBASE_SYSTEM_PROMPT,
    FILE_UPDATE_SYSTEM_PROMPT,
    CHAT_SYSTEM_PROMPT,
)
from forge.config import config


class LocalBackend(ModelBackend):
    """
    Runs the fine-tuned Phi-3.5 Mini GGUF via llama-cpp-python.
    Loaded once at startup, cached in memory.
    """

    def __init__(self):
        self._llm = None
        self._load_model()

    def _load_model(self):
        try:
            from llama_cpp import Llama
            print(f"[Forge] Loading local model: {config.model.local_model_path}")
            self._llm = Llama(
                model_path   = config.model.local_model_path,
                n_ctx        = config.model.local_context,
                n_threads    = config.model.local_threads,
                n_gpu_layers = config.model.local_gpu_layers,
                verbose      = False,
            )
            print("[Forge] Local model loaded ✓")
        except ImportError:
            print("[Forge] llama-cpp-python not installed. Run: pip install llama-cpp-python")
            self._llm = None
        except FileNotFoundError:
            print(f"[Forge] Model file not found: {config.model.local_model_path}")
            print("[Forge] Run the training pipeline first, or set MODEL_BACKEND=together")
            self._llm = None

    async def health(self) -> dict:
        return {
            "backend": "local",
            "model"  : config.model.local_model_path,
            "status" : "ok" if self._llm else "model_not_loaded",
        }

    def _format_phi_prompt(self, system: str, messages: list[dict]) -> str:
        """Phi-3.5 Mini uses a specific chat template."""
        parts = [f"<|system|>\n{system}<|end|>"]
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            parts.append(f"<|{role}|>\n{content}<|end|>")
        parts.append("<|assistant|>")
        return "\n".join(parts)

    async def _stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        if not self._llm:
            yield '{"error": "Local model not loaded. Check LOCAL_MODEL_PATH in .env"}'
            return

        prompt = self._format_phi_prompt(system, messages)
        stream = self._llm(
            prompt,
            max_tokens  = 4096,
            temperature = 0.2,
            stream      = True,
            stop        = ["<|end|>", "<|user|>"],
        )
        for chunk in stream:
            token = chunk["choices"][0]["text"]
            if token:
                yield token

    async def generate_codebase(self, prompt, language="auto", extra_context="", stack=None) -> AsyncIterator[str]:
        user_msg = f"Build: {prompt}"
        if language != "auto":
            user_msg += f"\nLanguage: {language}"
        if extra_context:
            user_msg += f"\nExtra: {extra_context}"
        if stack and isinstance(stack, dict):
            fe = stack.get("fe", "react")
            be = stack.get("be", "none")
            db = stack.get("db", "none")
            user_msg += f"\nStack: frontend={fe}, backend={be}, database={db}"
        async for token in self._stream(CODEBASE_SYSTEM_PROMPT, [{"role": "user", "content": user_msg}]):
            yield token

    async def update_file(self, file_path, current_content, instruction, full_context=[]) -> AsyncIterator[str]:
        user_msg = f"File: {file_path}\n\nContent:\n```\n{current_content}\n```\n\nInstruction: {instruction}"
        async for token in self._stream(FILE_UPDATE_SYSTEM_PROMPT, [{"role": "user", "content": user_msg}]):
            yield token

    async def chat(
        self,
        messages,
        codebase_context=None,
        system_override: str | None = None,
        runtime_context: str | None = None,
    ) -> AsyncIterator[str]:
        system = system_override if system_override else CHAT_SYSTEM_PROMPT
        if runtime_context:
            system += f"\n\n{runtime_context}"
        async for token in self._stream(system, messages):
            yield token
