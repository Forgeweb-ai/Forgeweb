"""
forge_server.imagegen.smoke
=============================
Terminal smoke-test for the image-gen adapter, BYPASSING the DB + worker
queue. Use this to verify a fresh provider key against the adapter
implementation without spinning up forge-server.

USAGE

  export REPLICATE_API_TOKEN=r8_...
  python -m forge_server.imagegen.smoke --prompt "a bear cub watching mountains at sunset, painted illustration"

  # Other models in the registry:
  python -m forge_server.imagegen.smoke --provider replicate --model black-forest-labs/flux-1.1-pro --prompt "..."

WHAT IT DOES

  - Calls the adapter directly with the given prompt/model/size.
  - Saves the result to `./imagegen-smoke-<timestamp>.<ext>`.
  - Prints rough cost based on the registry's `price_usd_per_image`.

WHAT IT DOES NOT DO

  - No DB, no queue, no Forge auth, no user.
  - No Supabase upload — output lives where you ran the command.

Why a script instead of a unit test: a unit test that hits a real provider
would either (a) burn money on every CI run or (b) need a mock that's just
re-implementing the adapter. The smoke CLI is the cheapest reliable
verification path for "does this key actually work end-to-end against the
provider's current API."
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from forge_server.imagegen.adapters import get_adapter
from forge_server.imagegen.providers import lookup
from forge_server.imagegen.types import GenerateRequest, ImageGenError


# Map registry provider_id → env var that conventionally carries the key.
# Order matters only for the "what's set?" hint we print on missing key.
_ENV_VARS = {
    "replicate":  "REPLICATE_API_TOKEN",
    "openrouter": "OPENROUTER_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "google":     "GOOGLE_API_KEY",
}


def _resolve_api_key(provider_id: str, cli_value: str | None) -> str:
    """Prefer the CLI flag, fall back to the conventional env var.

    Why both: CLI is fine for ad-hoc one-offs; env is what the rest of the
    Forge dev workflow uses (docker-compose, .env files). Supporting both
    keeps the script honest in both contexts.
    """
    if cli_value:
        return cli_value
    env = _ENV_VARS.get(provider_id)
    if not env:
        sys.exit(f"error: unknown provider {provider_id!r}; pass --api-key explicitly")
    val = os.environ.get(env)
    if not val:
        sys.exit(f"error: no key — pass --api-key or set ${env}")
    return val


def _ext_for(content_type: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(content_type.lower(), "png")


async def _run(args: argparse.Namespace) -> int:
    entry = lookup(args.provider, args.model)
    if entry is None:
        # Custom AI on the fly — user passes --protocol + --base-url (mirrors
        # what the FE custom-AI form will eventually write to
        # user_settings.custom_image_providers).
        if not args.protocol:
            sys.exit(f"error: {args.provider}/{args.model} is not in the built-in registry; pass --protocol to use as a custom AI")
        protocol     = args.protocol
        base_url     = args.base_url
        size_default = "1024x1024"
        price_hint   = None
    else:
        protocol     = entry.protocol
        base_url     = args.base_url or entry.base_url
        size_default = entry.sizes[0]
        price_hint   = entry.price_usd_per_image

    size = args.size or size_default

    api_key = _resolve_api_key(args.provider, args.api_key)

    print(f"→ provider={args.provider} model={args.model} protocol={protocol} size={size}")
    if base_url:
        print(f"→ base_url={base_url}")
    if price_hint is not None:
        print(f"→ estimated cost: ${price_hint:.4f} per image (registry baseline)")

    adapter = get_adapter(protocol)
    request = GenerateRequest(
        model_id        = args.model,
        prompt          = args.prompt,
        size            = size,
        api_key         = api_key,
        base_url        = base_url,
        ref_image_bytes = None,
    )

    started = datetime.now(timezone.utc)
    try:
        image = await adapter(request)
    except ImageGenError as exc:
        print(f"adapter failed: {exc.category}: {exc.detail}")
        return 2

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    out_path = Path(args.out) if args.out else Path.cwd() / f"imagegen-smoke-{started:%Y%m%d-%H%M%S}.{_ext_for(image.content_type)}"
    out_path.write_bytes(image.data)
    print(f"✓ {len(image.data)} bytes ({image.content_type}) in {elapsed:.1f}s → {out_path}")
    if image.provider_request_id:
        print(f"  provider request id: {image.provider_request_id}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("USAGE")[0].strip())
    parser.add_argument("--provider", default="replicate",
                        help="registry provider_id (default: replicate)")
    parser.add_argument("--model", default="black-forest-labs/flux-schnell",
                        help="registry model_id (default: black-forest-labs/flux-schnell — cheapest)")
    parser.add_argument("--prompt", required=True,
                        help="text prompt")
    parser.add_argument("--size", default=None,
                        help="WxH, e.g. 1024x1024 (default: first size in registry)")
    parser.add_argument("--api-key", default=None,
                        help="provider API key; falls back to provider's conventional env var")
    parser.add_argument("--protocol", default=None,
                        help="adapter protocol (replicate|openrouter_chat|openai_images|google_imagen); inferred from registry when --provider/--model is a built-in")
    parser.add_argument("--base-url", default=None,
                        help="endpoint base URL for protocols that take one (openrouter_chat, openai_images, …)")
    parser.add_argument("--out", default=None,
                        help="output filepath (default: ./imagegen-smoke-<ts>.<ext>)")
    args = parser.parse_args()

    rc = asyncio.run(_run(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
