"""
forge_server/api/constants.py
==============================
Shared constants used across API modules.
Kept in a leaf module (no forge_server imports) to avoid circular imports.
"""

# Default LLM models for user settings.
# Both default to opencode-zen's free DeepSeek tier so a fresh signup lands on a
# usable model without supplying a key. See [[forge_v1_scope]] (double BYOK,
# free launch). User overrides are stored in user_settings.settings_json and
# round-tripped via /api/user/settings.
#
#   primary_model — main coding/chat agent (drives the composer + base agent)
#   design_model  — design-analyst + design-critic subagents
#   image_model   — AI image-generation provider/model (e.g. "openai/gpt-image-1")
#                   When empty/null the image-gen feature is OFF regardless of
#                   image_mode. Picked from the curated image-provider registry
#                   in forge_server/imagegen/providers.py, NOT from the opencode
#                   model list (image APIs don't share LLM SDK shape).
#   image_mode    — "off" | "auto" | "ask"
#                   off = never request images (default; keeps fresh accounts
#                         on zero added cost).
#                   auto = main agent decides when images are needed; no
#                          per-turn confirmation.
#                   ask = first time per session, FE prompts; sticky for the
#                         rest of the session. Avoids per-turn token cost of
#                         repeated confirmations (CLAUDE.md §3).
#
# These are kept as separate fields because they're tuned for different
# work: primary_model favours coding throughput, design_model favours visual
# reasoning, image_model is an entirely different (non-LLM) API surface.
# Collapsing would force one budget on three different jobs.
DEFAULT_PRIMARY_MODEL = "opencode/deepseek-v4-flash-free"
DEFAULT_DESIGN_MODEL  = "opencode/deepseek-v4-flash-free"
# image_model default is empty — feature is opt-in. We do NOT seed a default
# provider because every image provider costs the user real money on their
# BYOK key the first time the agent decides a page needs an illustration.
# Opt-in via Settings is the consent boundary.
DEFAULT_IMAGE_MODEL   = ""
DEFAULT_IMAGE_MODE    = "off"
IMAGE_MODE_VALUES: frozenset[str] = frozenset({"off", "auto", "ask"})

# Hard cap on how many models a single user can enable. Bounds the JSON
# payload size on every settings round-trip and the PATCH validation cost.
# 500 is generously above realistic usage (a user with 5 providers × ~20
# models each is at 100); existing free/paid catalogs are nowhere near this.
# CLAUDE.md §2 "Bound everything" — without a ceiling a malformed/abusive
# client could store an arbitrarily large list and inflate every page-load
# response × all containers.
MAX_ENABLED_MODELS = 500

SETTINGS_DEFAULTS = {
    "primary_model":    DEFAULT_PRIMARY_MODEL,
    "design_model":     DEFAULT_DESIGN_MODEL,
    "image_model":      DEFAULT_IMAGE_MODEL,
    "image_mode":       DEFAULT_IMAGE_MODE,
    # Per-user model visibility allowlist. List of "<providerID>/<modelID>"
    # strings. Semantics:
    #   []        → use the FE's default policy (opencode-zen free visible,
    #               paid hidden). Fresh users start here.
    #   non-empty → strict allowlist: ONLY listed models are visible in the
    #               model picker. Other models stay registered (for
    #               primary_model/design_model resolution) but the picker
    #               hides them.
    # Replaces the legacy localStorage `model.v1` store — that was browser-
    # local, so toggles vanished across devices, browsers, and quota sweeps.
    # See models.tsx visible() for the resolver and dialog-manage-models.tsx
    # for the toggle UI.
    "enabled_models":   [],
    # Per-user custom provider definitions. Maps providerID → opencode-shaped
    # provider config: { name, npm, options: {baseURL, ...}, models: {...},
    # headers?: {...} }. API keys for these providers live in
    # user_provider_keys.key_enc (encrypted) — never inside this object.
    # opencode reads this map per-session via /api/internal/custom-providers
    # (mirrors the /agent-model resolver). Default is an empty map so users
    # start with no custom providers.
    "custom_providers": {},
    # Per-user custom IMAGE provider definitions. Same idea as
    # custom_providers but for image-gen models — lets a user point Forge
    # at any image endpoint (self-hosted, internal gateway, a provider the
    # built-in registry hasn't added yet). Keyed by "<provider_id>/<model_id>"
    # so it can be addressed directly via user_settings.image_model. Value
    # is an ImageModel-shaped record (see imagegen/providers.py
    # custom_entry_from_dict for the accepted fields). API keys live in
    # user_provider_keys.key_enc — referenced by required_key_provider here,
    # never embedded. Empty by default so feature is purely opt-in.
    "custom_image_providers": {},
}

# Providers Forge owns and controls platform-wide. Users may not redefine
# these via the custom-provider flow — letting them would let a user point
# `anthropic` at an arbitrary baseURL and bypass the forge-llm-proxy (cost
# accounting, sanitization, rate-card lookup all live behind the proxy).
# Add a new entry here whenever a new provider is baked into the platform
# template at forge-opencode-config/opencode.json.
PLATFORM_PROVIDER_IDS: frozenset[str] = frozenset({
    "anthropic", "moonshot", "kimi", "google", "opencode",
})

# Maps opencode agent names → user_settings field that holds the agent's model.
# Used by /api/internal/agent-model so the opencode fork can resolve
# `model: "__FORGE_USER_SETTING__"` at task-dispatch time. Add a new entry here
# whenever a new user-configurable agent ships (no opencode-fork change needed
# beyond the agent definition itself).
AGENT_SETTING_KEYS: dict[str, str] = {
    "design-analyst": "design_model",
    "design-critic":  "design_model",
}
