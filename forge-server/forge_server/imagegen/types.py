"""
forge_server.imagegen.types
============================
Shared dataclasses for the image-gen subsystem.

Kept in a leaf module (no FastAPI / SQLAlchemy / provider-SDK imports) so the
smoke CLI and unit tests can import these without dragging the whole server.
"""
from __future__ import annotations

from dataclasses import dataclass


# Stable user-visible error categories. Worker maps every provider failure
# into one of these strings before writing to `image_jobs.error`. The FE
# decides what message to show — keeping the category set tiny + stable
# means we don't need a coordinated FE+BE deploy whenever a provider invents
# a new error string. NEVER add provider-specific values here; map them in
# the adapter.
#
# Why `quota` is distinct from `rate_limit`: rate_limit is *transient* (try
# again in a moment), quota is *persistent* (account out of credit / over
# monthly cap — user has to top up or wait until reset). The FE renders
# those completely differently; collapsing them would hide a billing
# blocker behind a "try again" UX that never recovers on its own.
ERROR_RATE_LIMIT     = "rate_limit"
ERROR_QUOTA          = "quota"
ERROR_AUTH           = "auth"
ERROR_CONTENT_POLICY = "content_policy"
ERROR_TIMEOUT        = "timeout"
ERROR_UNKNOWN        = "unknown"

ERROR_CATEGORIES = frozenset({
    ERROR_RATE_LIMIT, ERROR_QUOTA, ERROR_AUTH, ERROR_CONTENT_POLICY,
    ERROR_TIMEOUT, ERROR_UNKNOWN,
})


@dataclass(frozen=True)
class GenerateRequest:
    """Inputs the worker hands to an adapter.

    Kept narrow on purpose. Provider-specific knobs (samplers, schedulers,
    seeds, etc.) are NOT exposed at the worker boundary — they're either
    sensible defaults inside the adapter, or future-work fields that ride
    on a follow-up table column. Premature exposure of every provider's
    options would couple the queue schema to every API's quirks.

    `base_url` is for protocols that need an endpoint URL (anything OpenAI-
    flavoured: OpenRouter, custom AI gateways, self-hosted SD). Adapters for
    protocols with a fixed URL (Replicate) ignore it. None means "use the
    protocol's built-in default" — keeps built-in registry entries from
    having to repeat https://api.replicate.com over and over.
    """
    model_id:        str          # e.g. "black-forest-labs/flux-schnell"
    prompt:          str
    size:            str          # "WIDTHxHEIGHT", e.g. "1024x1024"
    api_key:         str          # plaintext, request-scoped, never logged
    base_url:        str | None = None
    ref_image_bytes: bytes | None = None   # img-to-img source; None = txt-to-img


@dataclass(frozen=True)
class GeneratedImage:
    """One adapter result.

    `data` is the raw image bytes — we always materialize the file on the
    worker side rather than passing back a provider URL the FE would have
    to fetch separately. Reason: providers' result URLs typically expire in
    1-24h, and we don't want the user's deployed app's `<img>` tag to break
    a day later. Materializing pins the asset under Forge/Supabase storage
    we control the lifetime of.
    """
    content_type:        str             # "image/png" | "image/jpeg" | "image/webp"
    data:                bytes
    provider_request_id: str | None = None   # for support/debug, never user-facing


class ImageGenError(Exception):
    """Raised by adapters. Carries a stable category + optional detail."""
    def __init__(self, category: str, detail: str = "") -> None:
        if category not in ERROR_CATEGORIES:
            # Hard-fail: an adapter trying to invent a new category is a bug
            # that would leak provider-shaped strings into the FE. Better to
            # crash the job loudly than persist garbage.
            raise ValueError(f"unknown error category: {category!r}")
        self.category = category
        self.detail   = detail
        super().__init__(f"{category}: {detail}" if detail else category)
