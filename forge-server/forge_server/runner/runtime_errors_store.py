"""
forge_server/runner/runtime_errors_store.py
============================================
Persistent ring buffer for runtime errors.

Two write paths feed this store:
  1. runner/log_watcher.py — server-side errors from `docker logs -f`
     (drizzle exceptions, 5xx server errors, unhandled rejections, etc.)
  2. api/runtime_errors_routes.py POST — browser-side errors forwarded by
     the in-iframe bridge (hydration mismatches, console.error, fetch !ok)

One read path:
  - api/runtime_errors_routes.py GET — the agent + the UI both pull from
    here. Cheap, no docker round-trip, no health probe.

Why Redis (and not the in-memory ring already in log_watcher.py):
  - Survives forge-server restarts. The watcher's in-memory ring is reset
    every time we deploy or the process crashes; the user expects the agent
    to still see errors from "a minute ago".
  - Visible across multiple forge-server instances if we ever scale
    horizontally. The watcher only knows about errors fired by ITS own
    pumped stream; the browser-side bridge can hit any forge-server pod.
  - Atomic LTRIM gives a true bounded ring without read-modify-write races.

Schema:
  Key   `forge:runtime-errors:{project_id}`
  Type  Redis LIST (left-pushed; index 0 is newest)
  Items JSON-encoded RuntimeError records
  Cap   RING_SIZE entries via LTRIM
  TTL   TTL_SEC seconds via EXPIRE (errors older than this aren't actionable;
        if the user comes back hours later, run /verify to repopulate)

If Redis is unreachable we fall back to a process-local dict so dev still
works without `docker compose up redis`. The fallback is lossy — that's
fine for dev, and explicit so we never quietly mask a prod outage.
"""
from __future__ import annotations

import json
import logging
import time
import hashlib
from collections import deque
from typing import Any

import redis.asyncio as aioredis

from forge_server.config import get_settings

log      = logging.getLogger("forge.runtime_errors")
settings = get_settings()

RING_SIZE = 50
TTL_SEC   = 60 * 30   # 30 min; user-actionable window
DEDUP_SEC = 5.0       # collapse identical errors within this window


def _key(project_id: str) -> str:
    return f"forge:runtime-errors:{project_id}"


def _fingerprint(record: dict[str, Any]) -> str:
    """Stable hash over the bits that define 'same error'."""
    parts = [
        record.get("source", ""),
        record.get("signature", ""),
        record.get("detail", ""),
        record.get("file", ""),
        str(record.get("line", "")),
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


# ── Redis singleton (lazy) ───────────────────────────────────────────────────

_redis: aioredis.Redis | None = None
_redis_unavailable = False


async def _get_redis() -> aioredis.Redis | None:
    """Returns a connected client, or None if Redis is unavailable.

    We cache the 'unavailable' verdict for one process lifetime so we don't
    pay a TCP timeout on every write when Redis is down — that would block
    the log watcher's pump and back-pressure docker stdout.
    """
    global _redis, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis is not None:
        return _redis
    try:
        client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        await client.ping()
        _redis = client
        log.info("runtime-errors: connected to Redis at %s", settings.redis_url)
        return client
    except Exception as e:
        _redis_unavailable = True
        log.warning(
            "runtime-errors: Redis unavailable (%s) — falling back to in-process store. "
            "Errors will not survive forge-server restart.", e,
        )
        return None


# ── In-process fallback ──────────────────────────────────────────────────────

_fallback: dict[str, deque[dict[str, Any]]] = {}
# Last-seen ts per (project_id, fingerprint) — used for dedup across both paths.
_last_seen: dict[tuple[str, str], float] = {}


# ── Public API ───────────────────────────────────────────────────────────────

async def record(project_id: str, record: dict[str, Any]) -> bool:
    """
    Add an error. Returns True if persisted, False if suppressed by dedup.

    `record` must include at least one of: signature, detail, message. We
    stamp `ts` (server-side, so clients can't time-skew the dedup) and
    `fingerprint` (stable id useful to the UI).
    """
    now = time.time()
    record = dict(record)
    record.setdefault("source", "server")
    record["ts"] = now
    record["fingerprint"] = _fingerprint(record)

    key_dedup = (project_id, record["fingerprint"])
    last = _last_seen.get(key_dedup, 0.0)
    if now - last < DEDUP_SEC:
        return False
    _last_seen[key_dedup] = now

    payload = json.dumps(record, default=str)

    client = await _get_redis()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.lpush(_key(project_id), payload)
            pipe.ltrim(_key(project_id), 0, RING_SIZE - 1)
            pipe.expire(_key(project_id), TTL_SEC)
            await pipe.execute()
            return True
        except Exception as e:
            log.warning("runtime-errors: redis write failed (%s); using fallback", e)

    buf = _fallback.setdefault(project_id, deque(maxlen=RING_SIZE))
    buf.appendleft(record)
    return True


async def list_errors(
    project_id: str, since_ts: float | None = None
) -> list[dict[str, Any]]:
    """Newest first. `since_ts` filters to errors strictly after that time."""
    client = await _get_redis()
    if client is not None:
        try:
            raw = await client.lrange(_key(project_id), 0, RING_SIZE - 1)
            items = [json.loads(r) for r in raw]
        except Exception as e:
            log.warning("runtime-errors: redis read failed (%s); using fallback", e)
            items = list(_fallback.get(project_id, []))
    else:
        items = list(_fallback.get(project_id, []))

    if since_ts is not None:
        items = [r for r in items if float(r.get("ts", 0)) > since_ts]
    return items


async def clear(project_id: str) -> int:
    """Drop everything for a project. Returns the count cleared."""
    n = 0
    client = await _get_redis()
    if client is not None:
        try:
            n = await client.llen(_key(project_id))
            await client.delete(_key(project_id))
        except Exception as e:
            log.warning("runtime-errors: redis clear failed (%s)", e)
    if project_id in _fallback:
        n = max(n, len(_fallback[project_id]))
        del _fallback[project_id]
    # Reset dedup so a recurring error can be re-recorded immediately.
    for k in list(_last_seen.keys()):
        if k[0] == project_id:
            del _last_seen[k]
    return n
