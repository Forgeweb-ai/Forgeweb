"""Model backend factory — returns the right backend from config."""
from forge.config import config
from forge.model.base import ModelBackend


_AI_BACKENDS = {
    "ai", "openai", "chatgpt", "deepseek", "anthropic", "claude",
    "gemini", "grok", "xai", "qwen", "custom",
}

def get_backend() -> ModelBackend:
    backend = config.model.backend.lower()
    if backend == "together":
        from forge.model.codegen import CodegenBackend
        return CodegenBackend()
    elif backend in _AI_BACKENDS:
        from forge.model.ai import AIBackend
        return AIBackend(provider=backend if backend != "ai" else None)
    elif backend == "local":
        from forge.model.local import LocalBackend
        return LocalBackend()
    else:
        raise ValueError(
            f"Unknown backend: '{backend}'. Set MODEL_BACKEND=ai, together, or local in .env"
        )
