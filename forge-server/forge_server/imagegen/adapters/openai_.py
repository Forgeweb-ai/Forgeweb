# Moved to openai_images.py (protocol-keyed naming). Re-export for safety —
# nothing in tree imports this directly, but a stale alembic-cache or local
# pyc could resolve here. Delete this file once we've cut a release.
from forge_server.imagegen.adapters.openai_images import generate  # noqa: F401
