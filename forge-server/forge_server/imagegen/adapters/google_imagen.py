"""
forge_server.imagegen.adapters.google_imagen
=============================================
STUB — Google Imagen via the AI Studio key. Follow-up turn.
"""
from __future__ import annotations

from forge_server.imagegen.types import (
    ERROR_UNKNOWN,
    GenerateRequest,
    GeneratedImage,
    ImageGenError,
)


async def generate(request: GenerateRequest) -> GeneratedImage:
    raise ImageGenError(
        ERROR_UNKNOWN,
        "Google Imagen adapter not yet wired — use Replicate or OpenRouter for now."
    )
