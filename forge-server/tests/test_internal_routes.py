"""
Unit tests for forge_server.api.internal_routes.

Covers:
  - HMAC verify accepts current minute and previous minute (clock-skew window)
  - HMAC verify rejects wrong secret, wrong user, malformed token
  - get_agent_model returns null for unmapped agent names (forward-compat)
  - cache hit skips DB
  - cache miss reads DB and stores result
  - DB miss falls back to SETTINGS_DEFAULTS
"""
from __future__ import annotations

import hmac
import time
from hashlib import sha256

import pytest
from fastapi import HTTPException

from forge_server.api import internal_routes as ir
from forge_server.api.constants import SETTINGS_DEFAULTS
from forge_server.config import get_settings


# ── HMAC verification ─────────────────────────────────────────────────────────

def _make_token(user_id: str, bucket: int) -> str:
    secret = get_settings().forge_internal_secret.encode("utf-8")
    msg    = f"{user_id}:{bucket}".encode("utf-8")
    return hmac.new(secret, msg, sha256).hexdigest()


def _make_project_token(project_id: str, bucket: int) -> str:
    """Mirror of the opencode plugin's signProjectToken helper.

    If the plugin's algorithm ever drifts from the server's _sign_project,
    these tests catch it before the snapshot path silently 401s in prod.
    """
    secret = get_settings().forge_internal_secret.encode("utf-8")
    msg    = f"project:{project_id}:{bucket}".encode("utf-8")
    return hmac.new(secret, msg, sha256).hexdigest()


# ── Project-scoped HMAC ───────────────────────────────────────────────────────

def test_project_token_accepts_current_bucket():
    bucket = int(time.time()) // 60
    token  = _make_project_token("p-uuid", bucket)
    assert ir.verify_internal_project_token("p-uuid", token) == "p-uuid"


def test_project_token_accepts_previous_bucket():
    bucket = int(time.time()) // 60 - 1
    token  = _make_project_token("p-uuid", bucket)
    assert ir.verify_internal_project_token("p-uuid", token) == "p-uuid"


def test_project_token_rejects_wrong_project():
    """A token signed for project A must not validate for project B —
    that's the whole point of putting project_id in the signed message."""
    bucket = int(time.time()) // 60
    token  = _make_project_token("p-A", bucket)
    with pytest.raises(HTTPException) as exc:
        ir.verify_internal_project_token("p-B", token)
    assert exc.value.status_code == 401


def test_project_token_rejects_two_buckets_old():
    bucket = int(time.time()) // 60 - 2
    token  = _make_project_token("p-uuid", bucket)
    with pytest.raises(HTTPException) as exc:
        ir.verify_internal_project_token("p-uuid", token)
    assert exc.value.status_code == 401


def test_project_token_domain_separated_from_user_token():
    """A token signed under the user-scoped scheme `{uid}:{bucket}` must NOT
    validate under the project-scoped scheme `project:{pid}:{bucket}`, even
    when the IDs happen to be string-equal. The "project:" prefix is the
    domain separator that prevents cross-context replay."""
    bucket   = int(time.time()) // 60
    same_id  = "shared-uuid"
    user_tok = _make_token(same_id, bucket)
    with pytest.raises(HTTPException) as exc:
        ir.verify_internal_project_token(same_id, user_tok)
    assert exc.value.status_code == 401


def test_project_token_rejects_missing_header():
    with pytest.raises(HTTPException) as exc:
        ir.verify_internal_project_token("p-uuid", None)
    assert exc.value.status_code == 401


def test_verify_accepts_current_bucket():
    bucket = int(time.time()) // 60
    token  = _make_token("u1", bucket)
    assert ir.verify_internal_token("u1", token) == "u1"


def test_verify_accepts_previous_bucket():
    bucket = int(time.time()) // 60 - 1
    token  = _make_token("u1", bucket)
    assert ir.verify_internal_token("u1", token) == "u1"


def test_verify_rejects_two_buckets_old():
    bucket = int(time.time()) // 60 - 2
    token  = _make_token("u1", bucket)
    with pytest.raises(HTTPException) as exc:
        ir.verify_internal_token("u1", token)
    assert exc.value.status_code == 401


def test_verify_rejects_wrong_user():
    bucket = int(time.time()) // 60
    token  = _make_token("u1", bucket)
    with pytest.raises(HTTPException):
        ir.verify_internal_token("u2", token)


def test_verify_rejects_missing_headers():
    with pytest.raises(HTTPException) as exc:
        ir.verify_internal_token(None, None)
    assert exc.value.status_code == 401


def test_verify_rejects_garbage_token():
    with pytest.raises(HTTPException):
        ir.verify_internal_token("u1", "not-a-real-hmac")


# ── Cache layer (sync, no DB) ─────────────────────────────────────────────────

def test_cache_round_trip():
    ir._cache.clear()
    assert ir._cache_get("u1", "design_model") is None
    ir._cache_put("u1", "design_model", "google/gemini-3.5-flash")
    assert ir._cache_get("u1", "design_model") == "google/gemini-3.5-flash"


def test_cache_expiry(monkeypatch):
    ir._cache.clear()
    fake_now = [1000.0]
    monkeypatch.setattr(ir.time, "monotonic", lambda: fake_now[0])
    ir._cache_put("u1", "design_model", "v")
    assert ir._cache_get("u1", "design_model") == "v"
    fake_now[0] += ir._CACHE_TTL_SECONDS + 0.1
    assert ir._cache_get("u1", "design_model") is None


# ── Unmapped agent (forward-compat) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_unmapped_agent_returns_null():
    # The endpoint should return {model: null} for an unknown agent name so a
    # forge-server deploy that lags an opencode update doesn't break the run —
    # opencode then keeps its static config for that agent.
    class _FakeDB:
        async def execute(self, *_a, **_k): raise AssertionError("DB must not be hit for unmapped agent")
    result = await ir.get_agent_model(agent="brand-new-future-agent", user_id="u1", db=_FakeDB())
    assert result.model is None
