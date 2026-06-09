"""
forge_server/runner/opencode_proxy.py
======================================
Phase 2 BYOK: proxies all browser→opencode traffic through forge-server so
the calling user's per-user provider keys can be attached as a request-scoped
header (`X-Forge-Auth`). Replaces the shared on-disk auth.json hack that
leaked keys across users on the single shared opencode process.

Path: /opencode/*  →  ${OPENCODE_URL}/*

Flow per request:
  1. Authenticate the user (JWT via current_user dependency).
  2. Resolve the user's decrypted provider keys from key_cache (DB on miss).
  3. Base64-encode the key map as JSON; set `X-Forge-Auth` header.
  4. Stream the request body to opencode.
  5. Stream opencode's response (status, headers, body — SSE-safe) back.

The proxy does NOT buffer the body. Chat / session streaming responses
(text/event-stream) pass through chunk-for-chunk so the FE's incremental
rendering works unchanged.

Security
--------
- Keys live decrypted in process memory only for the request duration plus
  the cache TTL (5 min). Never written to disk by this path.
- The header carries the user's keys to opencode INSIDE the cluster only —
  opencode is not exposed to the internet (see docker-compose.yml). If you
  expose opencode publicly later, gate it with mTLS first.
- Hop-by-hop headers (Connection, Transfer-Encoding, etc.) are stripped per
  RFC 7230 §6.1 to avoid corrupting the proxied response.

Scale shape
-----------
- One shared httpx.AsyncClient (HTTP/1.1 keep-alive pool) per forge-server
  process. Connection reuse keeps tail latency flat at high RPS.
- Per request cost: one cache lookup (sub-µs hot, ~1ms cold w/ DB hit),
  one base64+JSON encode (<100µs for ≤20 providers), one intra-network
  hop (~0.5-1ms in-cluster). Flat per container.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import time
from hashlib import sha256
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.api.auth import current_user, current_user_qs
from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import User
from forge_server.services.key_cache import cache, load_user_keys

log = logging.getLogger("forge.opencode_proxy")
settings = get_settings()

router = APIRouter(prefix="/opencode", tags=["opencode-proxy"])

# Single shared client. Long-lived; no per-request connect cost. Timeout is
# generous because chat streams can run for minutes.
_client: httpx.AsyncClient | None = None


# Defense-in-depth read timeout. This is a BACKSTOP for a fully-wedged opencode
# process, not the primary stall detector — that lives in opencode (the idle
# watchdog in session/processor.ts, default 30s, which surfaces a retry well
# inside this window so the SSE stream stays active). Kept generous so it never
# interrupts a healthy turn: opencode emits events continuously while working
# (tokens, tool updates, retry notices). Previously this was None (infinite),
# which let a dead upstream hold the SSE connection open forever → UI stuck on
# "Thinking". Tunable via env; <=0 restores the old unbounded behavior.
def _read_timeout() -> float | None:
    import os
    raw = os.environ.get("FORGE_PROXY_READ_TIMEOUT_S", "").strip()
    try:
        val = float(raw) if raw else 300.0
    except ValueError:
        val = 300.0
    return val if val > 0 else None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.opencode_url,
            timeout=httpx.Timeout(connect=10.0, read=_read_timeout(), write=60.0, pool=10.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
    return _client


# Hop-by-hop headers — must not be forwarded. RFC 7230 §6.1. We also drop
# content-length and content-encoding because:
#   - content-length: httpx (request) and uvicorn (response) recompute it.
#   - content-encoding: we ask the upstream NOT to compress (see Accept-Encoding
#     below) AND we hand StreamingResponse already-decoded bytes, so any
#     leftover content-encoding header from a misbehaving upstream would
#     make the browser try to gunzip plain bytes. Belt-and-braces strip.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}


def _filter_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _encode_forge_auth(key_map: dict[str, dict[str, str]]) -> str:
    """JSON-then-base64 to keep the value header-safe (no newlines/non-ASCII)."""
    return base64.b64encode(json.dumps(key_map, separators=(",", ":")).encode()).decode()


def _sign_user_id(user_id: str) -> str:
    """HMAC-SHA256(forge_internal_secret, f'{user_id}:{minute_bucket}'), hex.

    Mirror of internal_routes._sign — kept independent so the two modules
    don't import each other (proxy must boot even if internal_routes is
    disabled in some future config). 60-second window matches the verifier;
    don't change one without the other.
    """
    bucket = int(time.time()) // 60
    msg    = f"{user_id}:{bucket}".encode("utf-8")
    key    = settings.forge_internal_secret.encode("utf-8")
    return hmac.new(key, msg, sha256).hexdigest()


async def _resolve_forge_auth(user: User, db: AsyncSession) -> str:
    """
    Always returns an encoded `X-Forge-Auth` value, even when the user has no
    keys saved. The presence of the header (not the contents) is what tells
    opencode "this is a Forge BYOK request" and flips `Auth.ForgeMode`, which
    in turn suppresses platform env / config keys. Without this, a brand-new
    user would inherit the shared opencode container's process env keys.
    """
    keys = await cache().get(
        str(user.id),
        loader=lambda: load_user_keys(db, str(user.id)),
    )
    return _encode_forge_auth(keys)


async def _proxy(
    request: Request,
    path: str,
    user: User,
    db: AsyncSession,
) -> StreamingResponse:
    client = _get_client()

    # Build the forwarded request. Stream the incoming body straight through
    # so large multipart uploads / long-running chat bodies aren't buffered.
    fwd_headers = _filter_headers(dict(request.headers))
    # Always set the header — empty map ({}) is still a positive signal that
    # this is a per-user Forge request. See _resolve_forge_auth for the why.
    fwd_headers["x-forge-auth"] = await _resolve_forge_auth(user, db)

    # Per-user identity for opencode-side internal lookups (e.g. agent-model
    # resolver in tool/task.ts). The opencode `forge-user.ts` middleware
    # verifies the HMAC, exposes the user-id as a request-scoped Effect
    # service, and forwards both headers verbatim when calling back into
    # /api/internal/* on forge-server. See internal_routes.verify_internal_token.
    user_id_str = str(user.id)
    fwd_headers["x-forge-user-id"]        = user_id_str
    fwd_headers["x-forge-internal-token"] = _sign_user_id(user_id_str)

    # Force the upstream to send uncompressed. Two reasons:
    #   1. opencode's compressionLayer gzips by default. Gzip frames buffer
    #      multiple SSE events before producing output bytes, so the browser
    #      sees nothing until the buffer flushes — chat responses look "stuck"
    #      until the connection closes (or the user refreshes).
    #   2. We're on a localhost / intra-cluster hop. Compression saves nothing
    #      meaningful and just adds CPU + latency.
    # We decode + re-stream below, so any negotiated encoding here would also
    # need decompression first, which httpx only does in aiter_bytes mode.
    fwd_headers["accept-encoding"] = "identity"

    target_path = "/" + path if not path.startswith("/") else path
    query = request.url.query
    if query:
        target_path = f"{target_path}?{query}"

    req = client.build_request(
        method=request.method,
        url=target_path,
        headers=fwd_headers,
        content=request.stream(),
    )

    try:
        upstream = await client.send(req, stream=True)
    except httpx.ConnectError as exc:
        log.error("opencode unreachable at %s: %s", settings.opencode_url, exc)
        return StreamingResponse(
            iter([b'{"error":"opencode_unreachable"}']),
            status_code=502,
            media_type="application/json",
        )

    async def body_iter() -> AsyncIterator[bytes]:
        # aiter_bytes (not aiter_raw) so httpx decodes transfer-encoding and
        # any negotiated content-encoding. With Accept-Encoding=identity above,
        # this is effectively a pass-through but it ALSO unwraps the chunked
        # framing — critical because we stripped Transfer-Encoding from the
        # response above (it's hop-by-hop) and StreamingResponse will re-chunk
        # for the downstream connection. Sending already-chunked bytes through
        # a second chunker would double-frame the response and the browser
        # would buffer until close. chunk_size=None → yield as bytes arrive,
        # which is what SSE needs to flush per-event.
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=None):
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=_filter_headers(dict(upstream.headers)),
        media_type=upstream.headers.get("content-type"),
    )


# ── Routes ────────────────────────────────────────────────────────────────────

# EventSource (SSE) opens GETs without an Authorization header, so for GETs
# we also accept ?token=<jwt>. Non-GET requests use the strict Bearer dep.

@router.api_route(
    "/{path:path}",
    methods=["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy_non_get(
    path: str,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    return await _proxy(request, path, user, db)


@router.get("/{path:path}", include_in_schema=False)
async def proxy_get(
    path: str,
    request: Request,
    user: User = Depends(current_user_qs),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    return await _proxy(request, path, user, db)


async def shutdown() -> None:
    """Call from FastAPI lifespan to release the keep-alive pool cleanly."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
