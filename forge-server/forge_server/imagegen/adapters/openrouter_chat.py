"""
forge_server.imagegen.adapters.openrouter_chat
================================================
OpenRouter image generation via `/v1/chat/completions` with
`modalities:["image"]`.

Covers EVERY image model OpenRouter exposes — Grok Imagine, hosted Flux,
Gemini Image, Ideogram, etc. ONE key (the user's OpenRouter API key)
unlocks all of them; choice of model is per-request via the `model` field.

Wire shape (from openrouter.ai/docs/api/reference/overview, June 2026):

    POST https://openrouter.ai/api/v1/chat/completions
    Authorization: Bearer <OPENROUTER_API_KEY>
    HTTP-Referer:  <attribution URL>       (optional, recommended)
    X-OpenRouter-Title: Forge              (optional, recommended)

    {
      "model": "x-ai/grok-imagine-image-quality",
      "modalities": ["image"],
      "messages": [
        { "role": "user", "content": [
          { "type": "text", "text": "<prompt>" },
          { "type": "image_url", "image_url": {"url": "data:image/png;base64,..."} }  # optional, img2img
        ]}
      ]
    }

Response is OpenRouter's normalized chat-completion shape; the image lands
as a data URL inside `choices[0].message.content` — either as a string with
a `data:` URL or as a content-parts list with `image_url` entries. We
handle both because OpenRouter has shipped both shapes for different
provider integrations.

Why we hit the REST API directly and not the OpenAI SDK:
  - Saves the SDK's import cost on every worker process. The SDK is ~MB-
    scale and pulls Pydantic + httpx + types we already have.
  - SDK's `chat.completions.create` doesn't expose `modalities` as a typed
    field across versions; we'd have to use the `extra_body` escape hatch
    anyway. Raw HTTP is shorter AND clearer.

`base_url` defaults to OpenRouter's host. Custom AI gateways (user-defined
in user_settings.custom_image_providers) can point this at any
OpenRouter-compatible endpoint by setting their own base_url.
"""
from __future__ import annotations

import base64
import logging
import re

from forge_server.imagegen.types import (
    ERROR_AUTH,
    ERROR_CONTENT_POLICY,
    ERROR_QUOTA,
    ERROR_RATE_LIMIT,
    ERROR_TIMEOUT,
    ERROR_UNKNOWN,
    GenerateRequest,
    GeneratedImage,
    ImageGenError,
)

log = logging.getLogger("forge.imagegen.openrouter_chat")


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# Attribution headers help OpenRouter rank Forge on its leaderboards AND let
# their support correlate issues back to us. Both are optional but cheap.
_ATTRIBUTION_REFERER = "https://forge.dev"
_ATTRIBUTION_TITLE   = "Forge"


# Match a data URL like `data:image/png;base64,<payload>` non-greedily.
# Used to extract the bytes from whichever shape OpenRouter returns.
_DATA_URL = re.compile(r"^data:(?P<ct>[a-zA-Z0-9.+/-]+);base64,(?P<b64>.+)$", re.DOTALL)


def _extract_image(message_content) -> tuple[str, bytes]:
    """Find the first image data URL in the assistant message and decode it.

    OpenRouter's docs say the image comes back as a data URL inside the
    message content. Across providers we've seen two shapes:
      (a) content is a string that IS the data URL.
      (b) content is an array of content parts; one part is
          {type:"image_url", image_url:{url:"data:..."}}.

    Raises ImageGenError if neither shape contains a usable image.
    """
    candidates: list[str] = []

    if isinstance(message_content, str):
        candidates.append(message_content)
    elif isinstance(message_content, list):
        for part in message_content:
            if not isinstance(part, dict):
                continue
            url = (part.get("image_url") or {}).get("url") if part.get("type") == "image_url" else None
            if url:
                candidates.append(url)
            elif part.get("type") == "text" and isinstance(part.get("text"), str):
                # Some providers wedge the data URL inside a text part. Last
                # resort, but worth trying before failing.
                candidates.append(part["text"])

    for c in candidates:
        m = _DATA_URL.match(c.strip())
        if not m:
            continue
        try:
            data = base64.b64decode(m.group("b64"), validate=True)
        except Exception:
            continue
        return m.group("ct"), data

    raise ImageGenError(ERROR_UNKNOWN, "response did not include a decodable image data URL")


async def generate(request: GenerateRequest) -> GeneratedImage:
    import httpx

    base_url = (request.base_url or DEFAULT_BASE_URL).rstrip("/")
    endpoint = f"{base_url}/chat/completions"

    user_content: list[dict] = [{"type": "text", "text": request.prompt}]
    if request.ref_image_bytes is not None:
        b64 = base64.b64encode(request.ref_image_bytes).decode()
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    body = {
        "model":      request.model_id,
        "modalities": ["image"],
        "messages":   [{"role": "user", "content": user_content}],
    }

    headers = {
        "Authorization":      f"Bearer {request.api_key}",
        "Content-Type":       "application/json",
        "HTTP-Referer":       _ATTRIBUTION_REFERER,
        "X-OpenRouter-Title": _ATTRIBUTION_TITLE,
    }

    # Generous timeout: image-gen on OpenRouter can take 30–60s depending on
    # the upstream provider. POLL_TIMEOUT in the Replicate adapter is 120s
    # because Replicate uses an explicit poll loop; here we just wait on the
    # single request.
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        try:
            resp = await client.post(endpoint, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ImageGenError(ERROR_TIMEOUT, "openrouter request timed out") from exc
        except httpx.HTTPError as exc:
            raise ImageGenError(ERROR_UNKNOWN, f"network: {exc}") from exc

        if resp.status_code in (401, 403):
            raise ImageGenError(ERROR_AUTH, f"openrouter rejected key (status {resp.status_code})")
        # 402 = Payment Required. OpenRouter returns this when the account
        # has no credit OR the request exceeds the user's per-key cap.
        # Either way, persistent — do not auto-retry.
        if resp.status_code == 402:
            raise ImageGenError(ERROR_QUOTA, "openrouter account out of credit or over cap")
        if resp.status_code == 429:
            raise ImageGenError(ERROR_RATE_LIMIT, "openrouter rate-limited the request")
        if resp.status_code >= 400:
            # Surface a compact slice of the body. Truncated so a verbose
            # upstream error doesn't blow up the `image_jobs.error` column or
            # leak a key OpenRouter happens to echo back (unlikely but cheap
            # to defend against).
            snippet = resp.text[:160].replace(request.api_key, "<redacted>")
            raise ImageGenError(ERROR_UNKNOWN, f"chat completion failed: {resp.status_code} {snippet}")

        body_json = resp.json()

        # OpenRouter envelope: top-level "error" wins if present, even on 200.
        if isinstance(body_json.get("error"), dict):
            err_msg = (body_json["error"].get("message") or "").lower()
            if "nsfw" in err_msg or "content" in err_msg or "policy" in err_msg or "safety" in err_msg:
                raise ImageGenError(ERROR_CONTENT_POLICY, "content policy")
            raise ImageGenError(ERROR_UNKNOWN, f"openrouter error: {err_msg[:120]}")

        choices = body_json.get("choices") or []
        if not choices:
            raise ImageGenError(ERROR_UNKNOWN, "response had no choices")

        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if content is None:
            raise ImageGenError(ERROR_UNKNOWN, "response message had no content")

        # `finish_reason: content_filter` also indicates policy block —
        # check it before trying to decode an image that won't be there.
        finish = (choices[0] or {}).get("finish_reason")
        if finish == "content_filter":
            raise ImageGenError(ERROR_CONTENT_POLICY, "content policy")

        content_type, data = _extract_image(content)
        request_id = body_json.get("id")
        return GeneratedImage(
            content_type        = content_type,
            data                = data,
            provider_request_id = request_id,
        )
