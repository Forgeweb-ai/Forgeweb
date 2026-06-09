"""
forge_server/services/key_cache.py
===================================
In-memory cache of decrypted per-user provider keys.

Why this exists
---------------
Every proxied opencode call needs the calling user's plaintext provider keys
to inject as the `X-Forge-Auth` header. Going to Postgres + Fernet-decrypting
on every request would add ~1–3ms of avoidable work to every chat/session/
provider call. Keys are tiny and change rarely, so an in-memory LRU keyed by
user_id is a near-free win.

Design
------
- Bounded size (`max_users`): hard ceiling so a runaway user count can't
  exhaust process memory. LRU eviction once full.
- TTL: short, so a stolen / rotated DB key naturally falls out without
  requiring perfect invalidation everywhere.
- Explicit invalidation: provider_routes.py POST/DELETE call `invalidate()`
  immediately on write so the next request sees fresh keys without waiting
  out the TTL.
- Async-safe: a single `asyncio.Lock` guards the dict. Reads are O(1); the
  lock window is microseconds. At 100k+ containers each instance still serves
  many concurrent users, and the lock never crosses an `await` outside the
  critical section.

Scale shape
-----------
Per active user: ~200–400 bytes (a handful of provider IDs + 60-char keys).
At 10k concurrent active users on one forge-server replica: ~4MB. Flat —
does not grow with conversation length or request rate.

NOT a store. The source of truth is `user_provider_keys` in Postgres. This
cache is allowed to be wrong briefly (TTL window); the proxy degrades to a
DB hit on miss.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("forge.key_cache")

# Type alias: { provider_id: { "type": "api", "key": "<plaintext>" } }
# Matches opencode's Auth.Info shape so the same JSON can be base64'd into
# the X-Forge-Auth header without further transformation.
KeyMap = dict[str, dict[str, str]]


class _Entry:
    __slots__ = ("keys", "expires_at")

    def __init__(self, keys: KeyMap, expires_at: float) -> None:
        self.keys = keys
        self.expires_at = expires_at


class KeyCache:
    """
    Per-user decrypted key cache. Methods:

      await get(user_id, loader)   → returns KeyMap, populating via loader on miss
      invalidate(user_id)          → drops a single user's entry (call on key write)
      clear()                      → drops everything (test helper)

    `loader` is an async callable that returns a fresh KeyMap from the DB.
    Keeping the loader as a parameter (rather than coupling this module to
    SQLAlchemy or the encryption helper) keeps the cache trivially testable.
    """

    def __init__(self, *, max_users: int = 10_000, ttl_seconds: float = 300.0) -> None:
        self._cache: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_users = max_users
        self._ttl = ttl_seconds

    async def get(
        self,
        user_id: str,
        loader: Callable[[], Awaitable[KeyMap]],
    ) -> KeyMap:
        now = time.monotonic()
        async with self._lock:
            entry = self._cache.get(user_id)
            if entry is not None and entry.expires_at > now:
                # LRU touch
                self._cache.move_to_end(user_id)
                return entry.keys
            if entry is not None:
                # Expired — drop and fall through to reload.
                del self._cache[user_id]

        # Lock dropped while we hit the DB so concurrent users don't queue.
        # A brief stampede on the same user_id is fine: both loaders return
        # the same data and the second wins idempotently.
        keys = await loader()

        async with self._lock:
            self._cache[user_id] = _Entry(keys=keys, expires_at=time.monotonic() + self._ttl)
            self._cache.move_to_end(user_id)
            # Evict oldest if over budget.
            while len(self._cache) > self._max_users:
                self._cache.popitem(last=False)
        return keys

    def invalidate(self, user_id: str) -> None:
        # Sync method — callable from non-async write paths. The lock is only
        # needed for read/insert ordering; a stray delete during a refresh is
        # safe (worst case: an extra DB hit on the next call).
        self._cache.pop(user_id, None)

    def clear(self) -> None:
        self._cache.clear()


# ── Singleton + DB loader ────────────────────────────────────────────────────

_singleton = KeyCache()


def cache() -> KeyCache:
    return _singleton


async def load_user_keys(db: "AsyncSession", user_id: str) -> KeyMap:
    """
    Fetch the user's provider keys from `user_provider_keys`, decrypt each,
    and return as a KeyMap. Errors on a single bad row are logged and that
    row is skipped — the rest of the user's keys still load.
    """
    # Import here to avoid a module-load cycle with provider_routes (which
    # depends on this cache via invalidate()) and to keep this module
    # importable in environments without sqlalchemy installed (e.g. unit
    # tests for the in-memory cache portion).
    from sqlalchemy import select
    from forge_server.api.config_routes import _fernet
    from forge_server.db.models import UserProviderKey

    result = await db.execute(
        select(UserProviderKey).where(UserProviderKey.user_id == user_id)
    )
    rows = result.scalars().all()
    fernet = _fernet()
    out: KeyMap = {}
    for r in rows:
        try:
            plaintext = fernet.decrypt(r.key_enc.encode()).decode()
        except Exception as exc:
            log.warning(
                "Could not decrypt key for user=%s provider=%s: %s",
                user_id, r.provider_id, exc,
            )
            continue
        out[r.provider_id] = {"type": "api", "key": plaintext}
    return out
