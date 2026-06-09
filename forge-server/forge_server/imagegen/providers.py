"""
forge_server.imagegen.providers
================================
Curated registry of image-generation models Forge knows how to call.

Architecture
------------
Each entry carries a `protocol` that names the WIRE SHAPE used to call it,
not the provider. Adapters are keyed by protocol, so adding a new provider
that speaks an already-supported protocol = ONE entry in this file. Zero
adapter code.

Supported protocols (see `adapters/`):
  - `replicate`        — Replicate prediction-poll API
  - `openrouter_chat`  — OpenRouter `/v1/chat/completions` with
                         `modalities:["image"]`; covers every image model
                         OpenRouter exposes (Grok Imagine, hosted Flux, etc.)
  - `openai_images`    — OpenAI `/v1/images/generations` (DALL·E, gpt-image-1)
  - `google_imagen`    — Google AI Studio `:generateContent`

Why a hand-maintained registry instead of "ask the provider what they support":
  1. Image APIs do NOT share a common SDK or capability surface. Auto-
     discovery would be a per-provider RPC dance with no upside — the list
     of useful image models is small and changes slowly.
  2. We need more than "exists" — pricing, supported sizes, img-to-img
     support, and which user_provider_keys row unlocks it. None of that is
     reliably available via provider introspection.
  3. Showing a model the user can't actually call (because they have no key
     for the underlying provider) is the worst possible UX. Registry +
     `available_for()` lets the Settings UI only ever show usable models.

Adding a new model = appending one ImageModel entry below. No DB migration,
no FE change beyond the model showing up in the picker the next page load.

Cost shape: this module is pure-Python data, no imports of provider SDKs.
At 100k+ containers the registry sits in process memory once (~few KB), zero
per-request allocation. Worker imports the adapter lazily only when a job
actually targets a given protocol, so an unused adapter never pays the
import-cost tax.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# Stable protocol identifiers. Adapters register against these.
PROTOCOL_REPLICATE        = "replicate"
PROTOCOL_OPENROUTER_CHAT  = "openrouter_chat"
PROTOCOL_OPENAI_IMAGES    = "openai_images"
PROTOCOL_GOOGLE_IMAGEN    = "google_imagen"

PROTOCOLS = frozenset({
    PROTOCOL_REPLICATE,
    PROTOCOL_OPENROUTER_CHAT,
    PROTOCOL_OPENAI_IMAGES,
    PROTOCOL_GOOGLE_IMAGEN,
})


@dataclass(frozen=True)
class ImageModel:
    """One row in the curated catalog.

    `provider_id` + `model_id` are the registry primary key. The string
    `f"{provider_id}/{model_id}"` is what gets stored in
    `user_settings.image_model` and round-tripped through the API — same
    convention as primary_model / design_model.

    `required_key_provider` names the `user_provider_keys.provider_id` row
    the worker reads to authenticate against the provider. For platforms
    where the user's existing LLM key also unlocks image-gen (OpenAI,
    Google), this matches an LLM provider id; for image-only platforms
    (Replicate, OpenRouter, Fal) it's a new key the user adds. The Settings
    UI uses this to either hide the model OR show "needs <provider> key →
    add one".

    `protocol` selects the adapter. `base_url` overrides the protocol's
    default endpoint (relevant for protocols that take an arbitrary URL —
    custom AI gateways, self-hosted endpoints). Built-in entries leave
    base_url=None unless they target a non-default host.
    """
    provider_id:            str
    model_id:               str
    display_name:           str
    required_key_provider:  str
    protocol:               str
    supports_img2img:       bool
    sizes:                  tuple[str, ...]
    price_usd_per_image:    float
    description:            str
    base_url:               str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Catalog
# ─────────────────────────────────────────────────────────────────────────────
#
# Order matters: the Settings UI renders in this order, so put the most-
# universally-useful models first. We lead with the *cheapest* tier
# (Replicate Flux Schnell at $0.003) and the *broadest* tier (OpenRouter)
# so the picker tells a coherent story top-down.

IMAGE_MODELS: tuple[ImageModel, ...] = (
    # ── Replicate (cheapest tier; direct, no aggregator markup) ──────────────
    ImageModel(
        provider_id="replicate",
        model_id="black-forest-labs/flux-schnell",
        display_name="Replicate · Flux Schnell",
        required_key_provider="replicate",
        protocol=PROTOCOL_REPLICATE,
        supports_img2img=False,
        sizes=("1024x1024", "1024x1536", "1536x1024"),
        price_usd_per_image=0.003,
        description="Fastest, cheapest tier. Great default for prototyping landing-page art.",
    ),
    ImageModel(
        provider_id="replicate",
        model_id="black-forest-labs/flux-1.1-pro",
        display_name="Replicate · Flux 1.1 Pro",
        required_key_provider="replicate",
        protocol=PROTOCOL_REPLICATE,
        supports_img2img=True,
        sizes=("1024x1024", "1024x1536", "1536x1024"),
        price_usd_per_image=0.04,
        description="High-quality general-purpose model. Excellent for stylized landing-page art. Supports reference images.",
    ),

    # ── OpenRouter (broadest tier; one key → many models) ───────────────────
    # OpenRouter exposes image models via chat-completions with
    # modalities:["image"]. ONE key unlocks every entry below.
    # Pricing is set by OpenRouter, may shift; numbers reflect the published
    # rate at registry-write time and should be re-checked when adding.
    ImageModel(
        provider_id="openrouter",
        model_id="x-ai/grok-imagine-image-quality",
        display_name="OpenRouter · Grok Imagine (Quality)",
        required_key_provider="openrouter",
        protocol=PROTOCOL_OPENROUTER_CHAT,
        supports_img2img=True,
        sizes=("1024x1024", "1024x1536", "1536x1024"),
        # 1K-quality tier; 2K is ~$0.07. We expose only 1K-equivalent sizes
        # at v1 to keep the cost UI honest.
        price_usd_per_image=0.05,
        description="xAI's photoreal model via OpenRouter. Best for posters, ads, packaging — strong multilingual text rendering inside images.",
        base_url="https://openrouter.ai/api/v1",
    ),

    # ── OpenAI Images (existing LLM key reuse) ──────────────────────────────
    # Marked as openai_images protocol — adapter is stubbed in v1 but the
    # entry stays in the picker so users can see it's coming.
    ImageModel(
        provider_id="openai",
        model_id="gpt-image-1",
        display_name="OpenAI · GPT Image 1",
        required_key_provider="openai",
        protocol=PROTOCOL_OPENAI_IMAGES,
        supports_img2img=True,
        sizes=("1024x1024", "1024x1536", "1536x1024"),
        price_usd_per_image=0.04,
        description="OpenAI's current image model. Strong on illustration. Reuses your OpenAI key.",
    ),
    ImageModel(
        provider_id="openai",
        model_id="dall-e-3",
        display_name="OpenAI · DALL·E 3",
        required_key_provider="openai",
        protocol=PROTOCOL_OPENAI_IMAGES,
        supports_img2img=False,
        sizes=("1024x1024", "1024x1792", "1792x1024"),
        price_usd_per_image=0.04,
        description="Legacy OpenAI model. Text-to-image only.",
    ),

    # ── Google Imagen ────────────────────────────────────────────────────────
    ImageModel(
        provider_id="google",
        model_id="imagen-4",
        display_name="Google · Imagen 4",
        required_key_provider="google",
        protocol=PROTOCOL_GOOGLE_IMAGEN,
        supports_img2img=False,
        sizes=("1024x1024", "1408x768", "768x1408"),
        price_usd_per_image=0.04,
        description="Google's flagship image model. Best for photorealistic content. Reuses your Google key.",
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Lookups
# ─────────────────────────────────────────────────────────────────────────────

# Pre-indexed for O(1) lookup. The worker hits this on every job pick; keeping
# it as a dict avoids a linear scan even though the registry is tiny today —
# cheap insurance that this stays sub-linear if the catalog grows.
_BY_ID: dict[tuple[str, str], ImageModel] = {
    (m.provider_id, m.model_id): m for m in IMAGE_MODELS
}


def lookup(provider_id: str, model_id: str) -> ImageModel | None:
    """Return the BUILT-IN registry entry, or None if not registered.

    NOTE: custom user-defined entries live in `user_settings.custom_image_providers`
    and are resolved by the API/worker layer separately — this lookup is
    intentionally only for the built-in catalog so it stays a pure module
    (no DB dependency, trivially unit-testable).
    """
    return _BY_ID.get((provider_id, model_id))


def parse_settings_value(value: str) -> tuple[str, str] | None:
    """Parse the `user_settings.image_model` string into (provider, model).

    Same `"provider/model"` convention as primary_model / design_model. Model
    ids that themselves contain `/` (e.g. `black-forest-labs/flux-schnell`)
    are handled by splitting on the FIRST slash only — the provider segment
    in our registry never contains a slash.
    Returns None for empty / malformed values so the caller can treat "off"
    and "garbage" identically (both → feature disabled).
    """
    if not value:
        return None
    idx = value.find("/")
    if idx <= 0 or idx == len(value) - 1:
        return None
    return value[:idx], value[idx + 1:]


def available_for(connected_key_providers: Iterable[str]) -> list[ImageModel]:
    """Filter the catalog to entries whose required_key_provider is connected.

    `connected_key_providers` is the set of `user_provider_keys.provider_id`
    values for the user. Order of the result matches IMAGE_MODELS order so
    the FE picker stays deterministic.
    """
    have = {p.lower() for p in connected_key_providers}
    return [m for m in IMAGE_MODELS if m.required_key_provider.lower() in have]


# ─────────────────────────────────────────────────────────────────────────────
# Custom user-defined entries (lives in user_settings; this is the loader)
# ─────────────────────────────────────────────────────────────────────────────

def custom_entry_from_dict(data: dict) -> ImageModel | None:
    """Hydrate a user-defined custom_image_provider record into an ImageModel.

    Returns None and silently skips on a malformed entry — we'd rather hide a
    bad row from the picker than crash settings reads. The PATCH endpoint
    rejects malformed entries up front; this is the belt-and-braces layer
    for legacy rows.

    Required keys: provider_id, model_id, display_name, required_key_provider,
                   protocol.
    Optional:      base_url, sizes (list of "WxH"), price_usd_per_image,
                   supports_img2img, description.
    """
    try:
        protocol = data["protocol"]
        if protocol not in PROTOCOLS:
            return None
        sizes = tuple(data.get("sizes") or ("1024x1024",))
        return ImageModel(
            provider_id           = data["provider_id"],
            model_id              = data["model_id"],
            display_name          = data["display_name"],
            required_key_provider = data["required_key_provider"],
            protocol              = protocol,
            supports_img2img      = bool(data.get("supports_img2img", False)),
            sizes                 = sizes,
            price_usd_per_image   = float(data.get("price_usd_per_image", 0.0)),
            description           = data.get("description", "Custom AI provider."),
            base_url              = data.get("base_url"),
        )
    except (KeyError, TypeError, ValueError):
        return None
