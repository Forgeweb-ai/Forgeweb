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
#
# These are kept as two distinct fields because they're tuned for different
# work: primary_model favours coding throughput, design_model favours visual
# reasoning. Collapsing them would force one budget on both jobs.
DEFAULT_PRIMARY_MODEL = "opencode/deepseek-v4-flash-free"
DEFAULT_DESIGN_MODEL  = "opencode/deepseek-v4-flash-free"

SETTINGS_DEFAULTS = {
    "primary_model": DEFAULT_PRIMARY_MODEL,
    "design_model":  DEFAULT_DESIGN_MODEL,
}
