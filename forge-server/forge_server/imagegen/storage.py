"""
forge_server.imagegen.storage
==============================
Persist generated images INSIDE the user's project workspace so the
project's own dev server (Next.js / Vite / etc.) serves them natively
under `/images/<slot>.png`. No forge-server route, no host-bridging, no
absolute URLs.

Why this design (post-2026-06-04 refactor):
  Previous design wrote to `<forge_data>/projects/<id>/assets/generated/`
  and exposed assets via a forge-server `/forge-img/<slot>` route. That
  introduced an iframe-vs-forge-server host gap (browser fetched from
  `<project>.preview.lvh.me`, the route lived on `forge-server`), which
  required absolute URLs, a configurable public base URL, a redirect/
  stream resolver, path-traversal validation, and four separate bug
  fixes. Writing into the workspace eliminates ALL of that:

    - Workspace path on host:  /forge-data/users/.../projects/.../workspace
    - Bind-mounted into container at:  /app
    - Project's dev server serves /app/public/ as the static root
    - File at workspace/public/images/<slot>.png  →  served at
      <preview>/images/<slot>.png  with zero custom routing.

  Bonus: generated images travel with the project on export / zip /
  deploy. Users see them in their own file tree (no hidden platform
  storage).

Filename convention:
  Always `.png`, regardless of what the provider returned. Reasons:
    1. The agent embeds the JSX URL BEFORE the worker knows the
       provider's content-type — the filename has to be predictable.
    2. Browsers content-sniff anyway; a JPEG saved as `.png` renders
       fine in `<img>` tags.
    3. Most image-gen providers (Replicate Flux, OpenRouter, OpenAI
       gpt-image-1, Google Imagen) ship PNG by default or accept a
       `png` output-format hint, so re-encoding is rarely needed.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from forge_server.imagegen.types import GeneratedImage

log = logging.getLogger("forge.imagegen.storage")


# Subdir inside the project's public/ root. Kept short and conventional —
# users expect `public/images/` to exist in any Next.js/Vite project.
# Intentionally NOT `ai-generated/` or `forge-images/` — those name
# branded platform storage; the user requested unbranded layout.
_PUBLIC_IMAGES_SUBDIR = ("public", "images")


def _project_images_dir(workspace_path: str) -> Path:
    """Return (and create) <workspace>/public/images/.

    mkdir is idempotent + safe; the parent `public/` might or might not
    exist depending on the framework template, but the project's static
    serving will pick it up either way.
    """
    root = Path(workspace_path).joinpath(*_PUBLIC_IMAGES_SUBDIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_to_disk(*, workspace_path: str, slot_id: str, image: GeneratedImage) -> tuple[Path, str]:
    """Persist `image` under <workspace>/public/images/<slot>.png.

    Returns (absolute_path, served_url). served_url is what the
    project's dev server will resolve — a plain `/images/<slot>.png`,
    relative to the project's own origin.

    Write is atomic: .tmp + os.replace within the same filesystem.
    Same-filesystem is guaranteed because the workspace is a host
    directory; we're not crossing mount points.
    """
    dest = _project_images_dir(workspace_path) / f"{slot_id}.png"
    tmp  = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(image.data)
    os.replace(tmp, dest)
    log.info("imagegen: wrote %s (%d bytes, ct=%s)", dest, len(image.data), image.content_type)
    return dest, f"/images/{slot_id}.png"


async def store(*, workspace_path: str, slot_id: str, image: GeneratedImage) -> str:
    """High-level entry point. Returns the served_url (relative path).

    The relative-URL contract is what makes the whole workspace design
    work: the agent embeds `/images/<slot>.png` in JSX, the iframe
    resolves it against the project's own preview origin, the dev
    server serves the file from /app/public/images/. Nothing on the
    forge-server path is involved.
    """
    _, url = write_to_disk(workspace_path=workspace_path, slot_id=slot_id, image=image)
    return url
