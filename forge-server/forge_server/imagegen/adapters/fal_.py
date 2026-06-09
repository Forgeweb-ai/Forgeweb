# Fal removed from v1 catalog — protocol overlaps with openrouter_chat for
# the models we care about, and Fal-direct can land later via its own
# protocol if there's demand. See openai_.py for rationale.
from forge_server.imagegen.types import (
    ERROR_UNKNOWN,
    GenerateRequest,
    GeneratedImage,
    ImageGenError,
)


async def generate(request: GenerateRequest) -> GeneratedImage:
    raise ImageGenError(ERROR_UNKNOWN, "Fal direct adapter removed from v1 — use OpenRouter to access these models.")
