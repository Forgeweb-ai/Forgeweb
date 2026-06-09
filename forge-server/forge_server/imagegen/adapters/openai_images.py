"""
forge_server.imagegen.adapters.openai_images
=============================================
STUB — OpenAI `/v1/images/generations` (DALL·E, gpt-image-1). Lands in a
follow-up turn alongside the OpenAI-Images endpoint shape (`/generations`
for txt2img vs `/edits` for img2img).

Renamed from `openai_.py` so the file matches its protocol identifier.
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
        "OpenAI Images adapter not yet wired — use Replicate or OpenRouter for now."
    )
