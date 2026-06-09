"""
forge_server.imagegen.adapters.replicate
==========================================
Replicate adapter — covers `black-forest-labs/flux-schnell`,
`black-forest-labs/flux-1.1-pro`, and any other Replicate-hosted model the
registry exposes.

Why we hit the REST API directly instead of `replicate` SDK:
  - The SDK adds ~6MB of imports and a Pydantic dependency we already vendor
    transitively, but every byte counts × 100k containers.
  - Replicate's prediction API is 3 endpoints (create, get, download). Writing
    those against httpx is shorter than learning the SDK's quirks.
  - SDK ties us to its bug schedule; raw HTTP lets us pin error handling.

Endpoints used:
  POST   https://api.replicate.com/v1/models/{model}/predictions
  GET    https://api.replicate.com/v1/predictions/{id}
  GET    {output_url}   (returned in prediction.output as a URL or list[URL])

Pricing reference (flux-schnell): ~$0.003 per 1024x1024 image — by far the
cheapest entry in the registry, which is why this is the wire-first adapter.
"""
from __future__ import annotations

import asyncio
import logging

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

log = logging.getLogger("forge.imagegen.replicate")

REPLICATE_API_BASE = "https://api.replicate.com/v1"

# Bound the polling loop. Flux Schnell finishes in <5s typically; Pro can
# take 30s+. Cap at 120s — anything longer is a stuck provider and we'd
# rather fail the job (so the placeholder swap to fallback fires) than block
# a worker slot indefinitely.
POLL_TIMEOUT_SECONDS = 120.0
POLL_INTERVAL_SECONDS = 1.0


def _aspect_ratio_from_size(size: str) -> str:
    """Replicate's flux models take `aspect_ratio` (e.g. '1:1', '16:9'), not
    a width/height pair. Map our registry's `WxH` shape to the closest
    supported aspect ratio.

    Why a small lookup instead of a math.gcd call: Replicate only accepts a
    fixed set of aspect ratios per model; arbitrary ratios fail with a
    cryptic 422. Snapping to the known-good set is faster + more reliable.
    """
    mapping = {
        "1024x1024": "1:1",
        "1024x1536": "2:3",
        "1536x1024": "3:2",
        "1408x768":  "16:9",
        "768x1408":  "9:16",
        "1792x1024": "16:9",
        "1024x1792": "9:16",
    }
    return mapping.get(size, "1:1")


async def generate(request: GenerateRequest) -> GeneratedImage:
    # Lazy import: keeps process startup cheap when no Replicate job is in
    # flight. httpx is already a transitive dep, so this is a free import.
    import httpx

    aspect = _aspect_ratio_from_size(request.size)

    payload: dict = {
        "input": {
            "prompt":      request.prompt,
            "aspect_ratio": aspect,
            # output_format png so callers don't have to disambiguate jpeg vs
            # webp downstream. Small size cost vs jpeg, but the asset lives
            # once and is served from CDN — not a hot path.
            "output_format": "png",
        }
    }
    if request.ref_image_bytes is not None:
        # Replicate flux models accept `image` as a URL OR a data URI. We
        # avoid the public-URL round trip by inlining as a base64 data URI.
        # Reference images are bounded (<=10MB per registry policy), so the
        # inline payload stays sane.
        import base64
        b64 = base64.b64encode(request.ref_image_bytes).decode()
        payload["input"]["image"] = f"data:image/png;base64,{b64}"

    headers = {
        "Authorization": f"Bearer {request.api_key}",
        "Content-Type":  "application/json",
    }

    create_url = f"{REPLICATE_API_BASE}/models/{request.model_id}/predictions"

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        # 1. Create the prediction
        try:
            resp = await client.post(create_url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ImageGenError(ERROR_TIMEOUT, "create prediction timed out") from exc
        except httpx.HTTPError as exc:
            raise ImageGenError(ERROR_UNKNOWN, f"network: {exc}") from exc

        if resp.status_code in (401, 403):
            raise ImageGenError(ERROR_AUTH, f"replicate rejected key (status {resp.status_code})")
        # 402 = Payment Required (account out of credit). Persistent until
        # the user tops up; NEVER auto-retry — that would just burn through
        # the next top-up on the same failing prompt.
        if resp.status_code == 402:
            raise ImageGenError(ERROR_QUOTA, "replicate account out of credit")
        if resp.status_code == 429:
            raise ImageGenError(ERROR_RATE_LIMIT, "replicate rate-limited the request")
        if resp.status_code >= 400:
            raise ImageGenError(ERROR_UNKNOWN, f"create failed: {resp.status_code} {resp.text[:160]}")

        prediction = resp.json()
        prediction_id = prediction.get("id")
        if not prediction_id:
            raise ImageGenError(ERROR_UNKNOWN, "create response missing id")

        # 2. Poll until terminal state
        get_url = f"{REPLICATE_API_BASE}/predictions/{prediction_id}"
        elapsed = 0.0
        while True:
            if elapsed >= POLL_TIMEOUT_SECONDS:
                raise ImageGenError(ERROR_TIMEOUT, f"replicate prediction {prediction_id} did not finish in {POLL_TIMEOUT_SECONDS}s")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

            try:
                poll = await client.get(get_url, headers=headers)
            except httpx.TimeoutException:
                # Single transient timeout shouldn't fail the whole job —
                # the next poll cycle will catch up.
                continue

            if poll.status_code >= 500:
                # Same logic — provider hiccup, try again next cycle.
                continue
            if poll.status_code == 429:
                # Back off one extra cycle when rate-limited on the poll side.
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            if poll.status_code >= 400:
                raise ImageGenError(ERROR_UNKNOWN, f"poll failed: {poll.status_code} {poll.text[:160]}")

            body = poll.json()
            status = body.get("status")
            if status in ("starting", "processing"):
                continue
            if status == "succeeded":
                output = body.get("output")
                # Output can be a single URL string OR a list of URLs.
                if isinstance(output, list):
                    if not output:
                        raise ImageGenError(ERROR_UNKNOWN, "succeeded with empty output list")
                    output_url = output[0]
                elif isinstance(output, str):
                    output_url = output
                else:
                    raise ImageGenError(ERROR_UNKNOWN, f"unexpected output shape: {type(output).__name__}")

                # 3. Download the bytes
                try:
                    dl = await client.get(output_url, timeout=httpx.Timeout(60.0, connect=10.0))
                except httpx.TimeoutException as exc:
                    raise ImageGenError(ERROR_TIMEOUT, "downloading output timed out") from exc
                if dl.status_code != 200:
                    raise ImageGenError(ERROR_UNKNOWN, f"download failed: {dl.status_code}")

                content_type = dl.headers.get("content-type", "image/png").split(";")[0].strip()
                return GeneratedImage(
                    content_type        = content_type,
                    data                = dl.content,
                    provider_request_id = prediction_id,
                )

            if status in ("failed", "canceled"):
                err = (body.get("error") or "").lower()
                # Replicate surfaces NSFW / safety errors as a string in the
                # `error` field. Map known phrases to the content-policy
                # category so the FE can render the right "try a different
                # prompt" hint.
                if "nsfw" in err or "safety" in err or "policy" in err:
                    raise ImageGenError(ERROR_CONTENT_POLICY, "content policy")
                raise ImageGenError(ERROR_UNKNOWN, f"prediction {status}: {err[:120]}")

            # Unknown status — log and continue polling; the API may have
            # introduced a new intermediate state we shouldn't crash on.
            log.warning("replicate prediction %s unknown status: %s", prediction_id, status)
