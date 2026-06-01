"""
forge_server.storage
====================
Workspace snapshot + rehydrate against Supabase Storage.

See [[forge_storage_architecture]] for the cold/warm/hot model these
modules implement. Quick map:
  - supabase_storage : thin async client over the Supabase Storage REST API
  - snapshots        : create / list / rehydrate-from snapshot tarballs
  - worker           : background loop that snapshots dirty projects
"""
from forge_server.storage.snapshots import (
    create_snapshot,
    latest_snapshot_for,
    rehydrate_from_latest_snapshot,
)
from forge_server.storage.supabase_storage import SupabaseStorageClient

__all__ = [
    "create_snapshot",
    "latest_snapshot_for",
    "rehydrate_from_latest_snapshot",
    "SupabaseStorageClient",
]
