"""
forge_server.imagegen.adapters
===============================
Protocol-keyed adapters for the image-gen subsystem.

We dispatch by *protocol* (the wire shape), not provider, because many
providers share a protocol (every "OpenAI-compatible" hosted endpoint, every
"OpenRouter-via-chat-completions" model). Adding a new provider that speaks
an existing protocol = one ImageModel entry in `providers.py`, zero adapter
code.

Resolution is lazy: a worker that only ever picks up Replicate jobs MUST
NOT import the openrouter_chat / openai_images / google_imagen adapters.
Per CLAUDE.md §2 — at 100k+ containers, every avoided module import is real
RSS we don't pay for.
"""
from __future__ import annotations

from typing import Callable

from forge_server.imagegen.types import ERROR_UNKNOWN, ImageGenError


# Map protocol → "module_path:callable_name". Resolved on first use.
_ADAPTER_PATHS: dict[str, str] = {
    "replicate":       "forge_server.imagegen.adapters.replicate:generate",
    "openrouter_chat": "forge_server.imagegen.adapters.openrouter_chat:generate",
    "openai_images":   "forge_server.imagegen.adapters.openai_images:generate",
    "google_imagen":   "forge_server.imagegen.adapters.google_imagen:generate",
}

_cache: dict[str, Callable] = {}


def get_adapter(protocol: str) -> Callable:
    """Return the adapter callable for this protocol, importing on first call.

    Signature of the returned callable:
        async def generate(request: GenerateRequest) -> GeneratedImage

    Raises ImageGenError(ERROR_UNKNOWN, …) for an unregistered protocol so
    the worker can fail the job with a stable category instead of an
    ImportError trace.
    """
    if protocol in _cache:
        return _cache[protocol]

    path = _ADAPTER_PATHS.get(protocol)
    if not path:
        raise ImageGenError(ERROR_UNKNOWN, f"no adapter registered for protocol '{protocol}'")

    module_name, attr = path.split(":")
    try:
        module = __import__(module_name, fromlist=[attr])
    except ImportError as exc:
        raise ImageGenError(ERROR_UNKNOWN, f"adapter module {module_name} failed to import: {exc}") from exc

    fn = getattr(module, attr, None)
    if fn is None:
        raise ImageGenError(ERROR_UNKNOWN, f"adapter {path} missing callable {attr}")

    _cache[protocol] = fn
    return fn
