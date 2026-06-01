"""
Thin async client over the Supabase Storage REST API.

We use httpx (already a dep) rather than the official supabase-py client
because (a) we only need four operations and (b) the official client is
sync-only for storage, which would block the FastAPI event loop.

Reference: https://supabase.com/docs/reference/api/storage
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx

from forge_server.config import get_settings

log = logging.getLogger(__name__)

_settings = get_settings()

# Per-process memo of buckets we've already ensured. ensure_bucket() is hit
# on EVERY snapshot tick for EVERY project — that's hundreds of redundant
# Supabase round-trips per minute. Memoizing here makes ensure_bucket a
# no-op after the first successful call in the process, which is the only
# safe shape at 100k+ snapshot operations.
_ENSURED_BUCKETS: set[str] = set()

# Substrings in a Supabase Storage error body that mean "bucket already
# exists" — different Supabase versions return 400 with various phrasings
# instead of the documented 409. Lowercased for matching.
_DUPLICATE_BUCKET_MARKERS = (
    "already exists",
    "duplicate",
    "resource_already_exists",
    "bucketalreadyexists",
)


def _service_headers() -> dict[str, str]:
    """Headers for server-side operations using the service role key.
    The service role key bypasses RLS — never expose it to the browser.

    Defensive normalization: pydantic-settings strips quotes from .env
    file lines but NOT from raw environment variables. dev.sh used to
    capture the key via `supabase status -o env | cut -d= -f2-`, which
    leaves the value wrapped in literal double quotes. The resulting
    `Bearer "eyJ..."` header fails Supabase's JWT decode with the cryptic
    "JWS Protected Header is invalid". We strip whitespace and any
    surrounding quote pair here so downstream callers cannot trip on
    this again, no matter how the key was sourced.
    """
    key = (_settings.supabase_service_role_key or "").strip()
    # Drop a matching pair of surrounding quotes (",') but only if BOTH
    # ends carry one — never strip a single stray quote from inside.
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ('"', "'"):
        key = key[1:-1].strip()
    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not set. dev.sh should export it "
            "from `supabase status -o env` — make sure dev.sh ran fully and "
            "Supabase is up."
        )
    # JWTs are exactly three base64url segments separated by dots. If the
    # value we got back is shape-wrong, fail loud now — way better than
    # the 400/"JWS invalid" log spam at every snapshot tick.
    if key.count(".") != 2:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY does not look like a JWT "
            "(expected 3 dot-separated segments). Did the capture in "
            "dev.sh include stray quotes or whitespace? Re-run dev.sh."
        )
    return {
        "Authorization": f"Bearer {key}",
        "apikey":        key,
    }


class SupabaseStorageClient:
    """Async Supabase Storage client scoped to one bucket.

    Lifecycle:
      async with SupabaseStorageClient(bucket="forge-snapshots") as client:
          await client.ensure_bucket(public=False)
          await client.upload(key, data_bytes)
    """

    def __init__(self, bucket: str | None = None):
        self.bucket = bucket or _settings.snapshots_bucket
        self.base   = _settings.supabase_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SupabaseStorageClient":
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Bucket ────────────────────────────────────────────────────────────────

    async def ensure_bucket(self, *, public: bool = False) -> None:
        """Idempotent — creates the bucket if it doesn't exist.

        Network shape:
          - First call in this process for this bucket: 1 GET (existence
            check). If 200 → memoize + return. If non-200 → 1 POST to create.
          - Every subsequent call in this process: zero network — memoized.

        Why memoize: ensure_bucket is invoked on every snapshot tick for
        every project. Without the memo, hundreds of redundant Supabase
        round-trips fire per minute at modest project counts; at the 100k+
        target it's a self-DoS.

        Why duplicate-bucket detection: Supabase Storage's bucket-create
        endpoint returns 409 in newer versions and 400 with a "Duplicate"
        body in older ones. We accept both — what matters is the bucket
        exists after this call returns. Raising on 400-when-it-already-
        exists is what was producing the snapshot-failed log spam.
        """
        assert self._client is not None
        if self.bucket in _ENSURED_BUCKETS:
            return

        # Existence check first. GET /bucket/{name}: 200 + body if exists,
        # 4xx if not.
        try:
            r = await self._client.get(
                f"{self.base}/storage/v1/bucket/{self.bucket}",
                headers=_service_headers(),
            )
            if r.status_code == 200:
                _ENSURED_BUCKETS.add(self.bucket)
                return
        except httpx.HTTPError as e:
            # Network/timeout on the existence check: fall through to the
            # create attempt. If create also fails we'll raise with the
            # POST error which is more actionable than the GET timeout.
            log.debug("bucket existence check failed (%s); attempting create", e)

        # Create. Accept several "already exists" shapes as success.
        r = await self._client.post(
            f"{self.base}/storage/v1/bucket",
            headers={**_service_headers(), "Content-Type": "application/json"},
            json={"id": self.bucket, "name": self.bucket, "public": public},
        )
        if r.status_code in (200, 201, 409):
            _ENSURED_BUCKETS.add(self.bucket)
            log.info("Supabase Storage bucket ready: %s", self.bucket)
            return

        # Non-success: parse the body to detect "already exists" disguised
        # as 400 (older Supabase versions / supabase-storage-go).
        body_text = (r.text or "").lower()
        if r.status_code == 400 and any(m in body_text for m in _DUPLICATE_BUCKET_MARKERS):
            _ENSURED_BUCKETS.add(self.bucket)
            log.info(
                "Supabase Storage bucket %s already exists (detected via 400 body)",
                self.bucket,
            )
            return

        # Genuinely unexpected: surface it so the operator can see auth /
        # config issues. Include the bucket name so the log is actionable.
        log.warning(
            "ensure_bucket %s: unexpected response %d body=%s",
            self.bucket, r.status_code, (r.text or "")[:300],
        )
        r.raise_for_status()

    # ── Objects ───────────────────────────────────────────────────────────────

    async def upload(self, key: str, data: bytes,
                     *, content_type: str = "application/zstd") -> None:
        """Upload (overwrites if key exists via upsert=true)."""
        assert self._client is not None
        r = await self._client.post(
            f"{self.base}/storage/v1/object/{self.bucket}/{key}",
            headers={
                **_service_headers(),
                "Content-Type": content_type,
                "x-upsert":     "true",
            },
            content=data,
        )
        r.raise_for_status()

    async def download(self, key: str) -> bytes:
        assert self._client is not None
        r = await self._client.get(
            f"{self.base}/storage/v1/object/{self.bucket}/{key}",
            headers=_service_headers(),
        )
        r.raise_for_status()
        return r.content

    async def stream_download(self, key: str) -> AsyncIterator[bytes]:
        """Stream a large object in chunks. Caller iterates."""
        assert self._client is not None
        async with self._client.stream(
            "GET",
            f"{self.base}/storage/v1/object/{self.bucket}/{key}",
            headers=_service_headers(),
        ) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes(chunk_size=65536):
                yield chunk

    async def delete(self, key: str) -> None:
        assert self._client is not None
        r = await self._client.delete(
            f"{self.base}/storage/v1/object/{self.bucket}/{key}",
            headers=_service_headers(),
        )
        # 404 is fine — already gone
        if r.status_code not in (200, 404):
            r.raise_for_status()
