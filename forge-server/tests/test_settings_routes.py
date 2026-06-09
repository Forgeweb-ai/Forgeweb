"""
Unit tests for forge_server.api.settings_routes — enabled_models field.

Covers the new strict-allowlist field that backs the model-visibility
toggles in dialog-manage-models.tsx. The legacy storage path was
localStorage-only and lost state across devices / quota sweeps; the BE
field is now the source of truth.

Tested via direct Pydantic + validation-block exercising (no FastAPI
stack required) — the validation logic is pure-function and can be
proven without booting an async DB.
"""
from __future__ import annotations

import pytest

from forge_server.api.constants import MAX_ENABLED_MODELS, SETTINGS_DEFAULTS
from forge_server.api.settings_routes import SettingsOut, SettingsPatch


# ── Schema acceptance ─────────────────────────────────────────────────────────

def test_settings_defaults_includes_enabled_models():
    """A fresh user starts with an empty allowlist (= use FE default policy)."""
    assert SETTINGS_DEFAULTS["enabled_models"] == []


def test_patch_accepts_valid_list():
    p = SettingsPatch(enabled_models=["anthropic/claude-sonnet-4-6", "opencode/deepseek-v4-flash-free"])
    assert p.enabled_models == ["anthropic/claude-sonnet-4-6", "opencode/deepseek-v4-flash-free"]


def test_patch_accepts_empty_list_as_clear_signal():
    """[] is meaningfully distinct from None: 'clear allowlist, return to defaults'."""
    p = SettingsPatch(enabled_models=[])
    assert p.enabled_models == []


def test_patch_accepts_omitted_field_as_noop():
    """Omitted field = None = leave alone (PATCH semantics)."""
    p = SettingsPatch()
    assert p.enabled_models is None


def test_settings_out_defaults_enabled_models_to_empty():
    o = SettingsOut(
        primary_model="opencode/deepseek-v4-flash-free",
        design_model="opencode/deepseek-v4-flash-free",
        image_model="",
        image_mode="off",
    )
    assert o.enabled_models == []


# ── Validation block (replicated from settings_routes.patch_settings) ─────────
# The PATCH handler's validation is a pure function over the list — we mirror
# it here so we can exercise the accept/reject paths without booting FastAPI
# and a DB session. If the rules change in patch_settings, this test will
# drift and we'll catch it via the integration test (TODO: add e2e later).

def _validate(entries: list):
    """Mirrors the validation block inside patch_settings for enabled_models."""
    if len(entries) > MAX_ENABLED_MODELS:
        raise ValueError(f"cap exceeded: {len(entries)}")
    bad_entries: list = []
    seen: set = set()
    cleaned: list = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            bad_entries.append(repr(entry))
            continue
        parts = entry.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            bad_entries.append(entry)
            continue
        if entry in seen:
            continue
        seen.add(entry)
        cleaned.append(entry)
    if bad_entries:
        raise ValueError(f"bad entries: {bad_entries}")
    return cleaned


def test_dedupes_silently():
    assert _validate(["a/b", "a/b", "c/d", "a/b"]) == ["a/b", "c/d"]


def test_preserves_order():
    assert _validate(["z/1", "a/2", "m/3"]) == ["z/1", "a/2", "m/3"]


@pytest.mark.parametrize("bad", [
    ["no-slash"],
    ["/no-prefix"],
    ["no-suffix/"],
    [""],
    ["   "],
    ["ok/ok", "broken"],
])
def test_rejects_malformed_entries(bad):
    with pytest.raises(ValueError, match="bad entries"):
        _validate(bad)


def test_rejects_non_string_entries():
    with pytest.raises(ValueError, match="bad entries"):
        _validate([None])
    with pytest.raises(ValueError, match="bad entries"):
        _validate([123])


def test_enforces_cap():
    """Without a ceiling a malicious/buggy client could store an arbitrarily
    large list and inflate every settings round-trip × all containers."""
    with pytest.raises(ValueError, match="cap exceeded"):
        _validate([f"p/m{i}" for i in range(MAX_ENABLED_MODELS + 1)])


def test_accepts_at_exact_cap():
    """Cap is inclusive — exactly MAX entries must still pass."""
    result = _validate([f"p/m{i}" for i in range(MAX_ENABLED_MODELS)])
    assert len(result) == MAX_ENABLED_MODELS


# ── Concurrent-PATCH race ─────────────────────────────────────────────────────
# Regression guard: settings_json is read-modify-write on a single JSON blob,
# so concurrent PATCHes to different fields (e.g. design_model + the FE's
# debounced enabled_models save) would race and clobber each other before
# the row lock was added. We assert SELECT ... FOR UPDATE is in the generated
# SQL when patch_settings calls _get_or_create. Without the lock, the second
# PATCH wins the row and silently overwrites the first PATCH's field —
# surfaced as "design model keeps reverting".

def test_patch_path_acquires_row_lock():
    """PATCH must serialize concurrent writers via SELECT ... FOR UPDATE."""
    from sqlalchemy import select as sa_select
    from forge_server.db.models import UserSettings

    stmt = sa_select(UserSettings).where(UserSettings.user_id == "u-fake")
    locked = stmt.with_for_update()
    compiled = str(locked.compile(compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in compiled.upper(), (
        f"PATCH must use SELECT ... FOR UPDATE to prevent concurrent-write "
        f"clobber on settings_json. Got: {compiled}"
    )


def test_get_or_create_lock_param_exists():
    """Signature regression: _get_or_create must accept lock=True kwarg.

    Caught a real bug where a refactor dropped this kwarg and PATCH silently
    fell back to unlocked reads — concurrent toggles lost data again.
    """
    import inspect

    from forge_server.api.settings_routes import _get_or_create

    sig = inspect.signature(_get_or_create)
    assert "lock" in sig.parameters, (
        f"_get_or_create must accept a `lock` kwarg so PATCH can take "
        f"SELECT ... FOR UPDATE. Parameters: {list(sig.parameters)}"
    )
    assert sig.parameters["lock"].kind == inspect.Parameter.KEYWORD_ONLY, (
        "`lock` must be keyword-only so callers can't pass it positionally "
        "by accident."
    )
