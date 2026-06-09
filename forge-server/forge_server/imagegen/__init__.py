"""
forge_server.imagegen
======================
Image-generation subsystem for Forge.

Why this lives in its own package, not folded into `api/` or `services/`:
the image-gen pipeline is one logical feature with several parts (curated
registry, provider adapters, async worker, storage uploader). Co-locating
them prevents the "where do image things live?" sprawl that bit the design
agent earlier.

Public surface:
  providers.IMAGE_MODELS       — curated registry (read-only)
  providers.lookup(pid, mid)   — fast O(1) lookup for the worker
  providers.available_for(...) — filter the registry by user's connected keys
"""
