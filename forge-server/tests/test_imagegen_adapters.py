"""
Unit tests for the image-gen adapters.

These tests run with httpx.MockTransport so they exercise the FULL adapter
code path (URL construction, headers, payload shape, response parsing,
error mapping) without ever touching the network. The smoke CLI is the
companion check against real providers.

What we cover per adapter:
  - Happy path: request shape sent, response parsed, bytes returned
  - Auth error → ERROR_AUTH
  - Rate-limit → ERROR_RATE_LIMIT
  - Content-policy → ERROR_CONTENT_POLICY
  - Garbage response → ERROR_UNKNOWN (not crash)

What we do NOT cover here:
  - Real network latency / timeouts (smoke CLI's job)
  - Actual provider output quality (smoke CLI + human review)
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from forge_server.imagegen.adapters import replicate as replicate_adapter
from forge_server.imagegen.adapters import openrouter_chat as openrouter_adapter
from forge_server.imagegen.types import (
    ERROR_AUTH,
    ERROR_CONTENT_POLICY,
    ERROR_QUOTA,
    ERROR_RATE_LIMIT,
    ERROR_UNKNOWN,
    GenerateRequest,
    ImageGenError,
)


# A tiny valid PNG: 1x1 transparent pixel. Enough bytes that "did we decode
# something nonzero?" is a meaningful check.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _patch_httpx_client(monkeypatch, transport: httpx.MockTransport) -> None:
    """Force every `httpx.AsyncClient(...)` created inside an adapter to use
    our MockTransport. Cleaner than patching specific methods: the adapter
    code stays unchanged, and `client.get`/`client.post`/timeout settings
    all go through the transport like real httpx.
    """
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", _factory)


# ─────────────────────────────────────────────────────────────────────────────
# Replicate adapter
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replicate_happy_path(monkeypatch):
    """Verifies: POST shape, headers, poll until succeeded, output download."""
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        # 1. CREATE
        if request.method == "POST" and request.url.path.endswith("/predictions"):
            assert request.headers["authorization"] == "Bearer test-key"
            body = json.loads(request.content)
            assert body["input"]["prompt"] == "a bear"
            assert body["input"]["aspect_ratio"] == "1:1"
            assert body["input"]["output_format"] == "png"
            return httpx.Response(201, json={"id": "pred-xyz", "status": "starting"})
        # 2. POLL — return succeeded immediately
        if request.method == "GET" and request.url.path.endswith("/predictions/pred-xyz"):
            return httpx.Response(200, json={
                "id":     "pred-xyz",
                "status": "succeeded",
                "output": ["https://replicate.delivery/img.png"],
            })
        # 3. DOWNLOAD output
        if request.method == "GET" and "replicate.delivery" in str(request.url):
            return httpx.Response(200, content=_TINY_PNG,
                                  headers={"content-type": "image/png"})
        return httpx.Response(500, text=f"unexpected request: {request.method} {request.url}")

    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    # Short-circuit the poll sleep so the test is instant.
    monkeypatch.setattr(replicate_adapter, "POLL_INTERVAL_SECONDS", 0.0)

    image = await replicate_adapter.generate(GenerateRequest(
        model_id = "black-forest-labs/flux-schnell",
        prompt   = "a bear",
        size     = "1024x1024",
        api_key  = "test-key",
    ))
    assert image.content_type == "image/png"
    assert image.data == _TINY_PNG
    assert image.provider_request_id == "pred-xyz"
    # 3 requests in order: create, poll, download
    assert [r.method for r in seen_requests] == ["POST", "GET", "GET"]


@pytest.mark.asyncio
async def test_replicate_auth_error(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="invalid token")
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ImageGenError) as exc:
        await replicate_adapter.generate(GenerateRequest(
            model_id="m", prompt="p", size="1024x1024", api_key="bad",
        ))
    assert exc.value.category == ERROR_AUTH


@pytest.mark.asyncio
async def test_replicate_quota(monkeypatch):
    """402 = account out of credit; must be persistent (no retry) and
    distinct from rate_limit so the FE can render 'top up' guidance."""
    def handler(request):
        return httpx.Response(402, text='{"title":"Insufficient credit"}')
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ImageGenError) as exc:
        await replicate_adapter.generate(GenerateRequest(
            model_id="m", prompt="p", size="1024x1024", api_key="k",
        ))
    assert exc.value.category == ERROR_QUOTA


@pytest.mark.asyncio
async def test_replicate_rate_limited(monkeypatch):
    def handler(request):
        return httpx.Response(429, text="too many")
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ImageGenError) as exc:
        await replicate_adapter.generate(GenerateRequest(
            model_id="m", prompt="p", size="1024x1024", api_key="k",
        ))
    assert exc.value.category == ERROR_RATE_LIMIT


@pytest.mark.asyncio
async def test_replicate_content_policy(monkeypatch):
    """Replicate surfaces NSFW / policy errors as a `failed` status with the
    reason in the `error` field. We map known phrases to CONTENT_POLICY so
    the FE shows the right hint."""
    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json={"id": "p1", "status": "starting"})
        return httpx.Response(200, json={
            "id": "p1", "status": "failed",
            "error": "Detected NSFW content in prompt.",
        })
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    monkeypatch.setattr(replicate_adapter, "POLL_INTERVAL_SECONDS", 0.0)
    with pytest.raises(ImageGenError) as exc:
        await replicate_adapter.generate(GenerateRequest(
            model_id="m", prompt="p", size="1024x1024", api_key="k",
        ))
    assert exc.value.category == ERROR_CONTENT_POLICY


# ─────────────────────────────────────────────────────────────────────────────
# OpenRouter adapter
# ─────────────────────────────────────────────────────────────────────────────

def _data_url(png_bytes: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"


@pytest.mark.asyncio
async def test_openrouter_string_content(monkeypatch):
    """OpenRouter returning the image as a string with a data URL in content."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer or-test"
        # Attribution headers are present
        assert "http-referer" in {h.lower() for h in request.headers.keys()}
        body = json.loads(request.content)
        assert body["model"] == "x-ai/grok-imagine-image-quality"
        assert body["modalities"] == ["image"]
        # User message content shape
        msg = body["messages"][0]
        assert msg["role"] == "user"
        assert any(p.get("type") == "text" and p["text"] == "a poster" for p in msg["content"])
        return httpx.Response(200, json={
            "id": "gen-1",
            "choices": [{
                "message": {"role": "assistant", "content": _data_url(_TINY_PNG)},
                "finish_reason": "stop",
            }],
        })
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))

    image = await openrouter_adapter.generate(GenerateRequest(
        model_id = "x-ai/grok-imagine-image-quality",
        prompt   = "a poster",
        size     = "1024x1024",
        api_key  = "or-test",
    ))
    assert image.data == _TINY_PNG
    assert image.content_type == "image/png"
    assert image.provider_request_id == "gen-1"
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_openrouter_content_parts_list(monkeypatch):
    """OpenRouter returning content as an array with an image_url part."""
    def handler(request):
        return httpx.Response(200, json={
            "id": "gen-2",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Here is the image:"},
                        {"type": "image_url", "image_url": {"url": _data_url(_TINY_PNG)}},
                    ],
                },
                "finish_reason": "stop",
            }],
        })
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    image = await openrouter_adapter.generate(GenerateRequest(
        model_id="x-ai/grok-imagine-image-quality",
        prompt="x", size="1024x1024", api_key="k",
    ))
    assert image.data == _TINY_PNG


@pytest.mark.asyncio
async def test_openrouter_content_filter(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "id": "gen-3",
            "choices": [{
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "content_filter",
            }],
        })
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ImageGenError) as exc:
        await openrouter_adapter.generate(GenerateRequest(
            model_id="m", prompt="p", size="1024x1024", api_key="k",
        ))
    assert exc.value.category == ERROR_CONTENT_POLICY


@pytest.mark.asyncio
async def test_openrouter_envelope_error(monkeypatch):
    """OpenRouter's top-level `error` field — even with 200 OK — maps to
    a category. Policy phrases map to CONTENT_POLICY; rest → UNKNOWN."""
    def handler(request):
        return httpx.Response(200, json={
            "error": {"code": 400, "message": "NSFW content detected in user prompt"},
        })
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ImageGenError) as exc:
        await openrouter_adapter.generate(GenerateRequest(
            model_id="m", prompt="p", size="1024x1024", api_key="k",
        ))
    assert exc.value.category == ERROR_CONTENT_POLICY


@pytest.mark.asyncio
async def test_openrouter_quota(monkeypatch):
    def handler(request):
        return httpx.Response(402, text='{"error":"out of credit"}')
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ImageGenError) as exc:
        await openrouter_adapter.generate(GenerateRequest(
            model_id="m", prompt="p", size="1024x1024", api_key="k",
        ))
    assert exc.value.category == ERROR_QUOTA


@pytest.mark.asyncio
async def test_openrouter_auth_error(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="unauthorized")
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ImageGenError) as exc:
        await openrouter_adapter.generate(GenerateRequest(
            model_id="m", prompt="p", size="1024x1024", api_key="bad",
        ))
    assert exc.value.category == ERROR_AUTH


@pytest.mark.asyncio
async def test_openrouter_garbage_response(monkeypatch):
    """If the response is shaped like a chat completion but contains no
    decodable image, we fail with UNKNOWN instead of crashing."""
    def handler(request):
        return httpx.Response(200, json={
            "id": "gen-x",
            "choices": [{
                "message": {"role": "assistant", "content": "I cannot draw that."},
                "finish_reason": "stop",
            }],
        })
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ImageGenError) as exc:
        await openrouter_adapter.generate(GenerateRequest(
            model_id="m", prompt="p", size="1024x1024", api_key="k",
        ))
    assert exc.value.category == ERROR_UNKNOWN


@pytest.mark.asyncio
async def test_openrouter_custom_base_url(monkeypatch):
    """Custom AI path — user-defined base_url is honoured."""
    seen_urls: list[str] = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={
            "id": "g", "choices": [{
                "message": {"role": "assistant", "content": _data_url(_TINY_PNG)},
                "finish_reason": "stop",
            }],
        })
    _patch_httpx_client(monkeypatch, httpx.MockTransport(handler))

    await openrouter_adapter.generate(GenerateRequest(
        model_id="my-model", prompt="p", size="1024x1024", api_key="k",
        base_url="https://internal.example.com/api/v1",
    ))
    assert seen_urls[0] == "https://internal.example.com/api/v1/chat/completions"
