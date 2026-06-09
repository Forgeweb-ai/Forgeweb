"""
Unit tests for forge_server.services.key_cache.

Covers:
  - cache hit returns stored value without re-invoking loader
  - cache miss invokes loader and stores result
  - TTL expiry triggers reload
  - invalidate() drops a single user
  - max_users enforces LRU eviction
"""
from __future__ import annotations

import asyncio

import pytest

from forge_server.services.key_cache import KeyCache


@pytest.mark.asyncio
async def test_hit_skips_loader():
    cache = KeyCache(max_users=10, ttl_seconds=60.0)
    calls = 0
    async def loader():
        nonlocal calls
        calls += 1
        return {"anthropic": {"type": "api", "key": "k1"}}

    a = await cache.get("u1", loader)
    b = await cache.get("u1", loader)
    assert a == b == {"anthropic": {"type": "api", "key": "k1"}}
    assert calls == 1, "loader should not run on cache hit"


@pytest.mark.asyncio
async def test_invalidate_forces_reload():
    cache = KeyCache(max_users=10, ttl_seconds=60.0)
    calls = 0
    async def loader():
        nonlocal calls
        calls += 1
        return {"openai": {"type": "api", "key": f"v{calls}"}}

    await cache.get("u1", loader)
    cache.invalidate("u1")
    result = await cache.get("u1", loader)
    assert calls == 2
    assert result["openai"]["key"] == "v2"


@pytest.mark.asyncio
async def test_lru_eviction_bounded():
    cache = KeyCache(max_users=2, ttl_seconds=60.0)
    async def loader_for(uid):
        async def l():
            return {"p": {"type": "api", "key": uid}}
        return l

    await cache.get("u1", await loader_for("u1"))
    await cache.get("u2", await loader_for("u2"))
    await cache.get("u3", await loader_for("u3"))  # evicts u1
    # u1 must reload — counts loader invocation
    counter = 0
    async def reload():
        nonlocal counter
        counter += 1
        return {"p": {"type": "api", "key": "u1-new"}}
    await cache.get("u1", reload)
    assert counter == 1, "u1 should have been evicted and reloaded"


@pytest.mark.asyncio
async def test_ttl_expiry(monkeypatch):
    cache = KeyCache(max_users=10, ttl_seconds=0.01)
    calls = 0
    async def loader():
        nonlocal calls
        calls += 1
        return {"p": {"type": "api", "key": f"v{calls}"}}
    await cache.get("u1", loader)
    await asyncio.sleep(0.02)
    await cache.get("u1", loader)
    assert calls == 2
