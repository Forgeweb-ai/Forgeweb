"""
Unit tests for the content-addressed version store.

Scope: deterministic, pure-Python helpers only (hashing, walking, key
layout, label fallbacks). The DB + Supabase Storage integration paths
(create_version, restore_version) require a Postgres + Supabase Storage
harness that doesn't exist in this test suite yet — see the
test_versions_integration follow-up note.

What these tests catch:
  - Hash determinism regressions (same content, same hash, always)
  - SNAPSHOT_EXCLUDES drift between snapshots.py and versions.py
  - blob-key sharding accidents (e.g. losing the prefix shard would
    overwhelm a single Storage prefix at scale)
  - MAX_FILE_BYTES guard breakage (the size DoS would hit blobs storage)
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from forge_server.storage.versions import (
    MAX_FILE_BYTES,
    _blob_key,
    _hash_file_sync,
    _walk_workspace_sync,
)
from forge_server.storage.snapshots import SNAPSHOT_EXCLUDES


# ─────────────────────────────────────────────────────────────────────────────
# Hashing
# ─────────────────────────────────────────────────────────────────────────────

def test_hash_is_sha256_of_content(tmp_path: Path) -> None:
    """The hash must equal the canonical sha256 of the file bytes; if it
    drifts (e.g. someone swaps the algo), every existing manifest is
    invalidated and dedup silently breaks."""
    f = tmp_path / "hello.txt"
    payload = b"hello forge"
    f.write_bytes(payload)
    sha, size = _hash_file_sync(f)
    assert sha == hashlib.sha256(payload).hexdigest()
    assert size == len(payload)


def test_hash_is_deterministic_across_calls(tmp_path: Path) -> None:
    """Same content → same hash. Foundation of cross-project dedup."""
    f = tmp_path / "x"
    f.write_bytes(b"abc" * 100)
    a, _ = _hash_file_sync(f)
    b, _ = _hash_file_sync(f)
    assert a == b


def test_identical_content_different_paths_share_hash(tmp_path: Path) -> None:
    """Two files with identical bytes hash to the same sha — the property
    that lets us upload one blob and reference it from N manifest entries."""
    a = tmp_path / "a" / "config.json"
    b = tmp_path / "b" / "config.json"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(b'{"x": 1}')
    b.write_bytes(b'{"x": 1}')
    assert _hash_file_sync(a)[0] == _hash_file_sync(b)[0]


def test_hash_rejects_oversize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The size guard must fire BEFORE the full read completes, or it
    can't protect against bandwidth DoS at scale."""
    # Shrink the ceiling to keep the test fast; the production constant
    # is large enough that writing 25MB+ in a test is wasteful.
    monkeypatch.setattr("forge_server.storage.versions.MAX_FILE_BYTES", 1024)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 2048)
    with pytest.raises(ValueError, match="MAX_FILE_BYTES"):
        _hash_file_sync(big)


def test_max_file_bytes_is_sane() -> None:
    """If someone sets this to something tiny by mistake, EVERY snapshot
    breaks. Catch obvious misconfigs."""
    assert MAX_FILE_BYTES >= 1 * 1024 * 1024, "ceiling too low; typical assets won't snapshot"
    assert MAX_FILE_BYTES <= 100 * 1024 * 1024, "ceiling too high; one bad file can bloat the blob store"


# ─────────────────────────────────────────────────────────────────────────────
# Walking + excludes
# ─────────────────────────────────────────────────────────────────────────────

def test_walk_includes_regular_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const x = 1\n")
    (tmp_path / "README.md").write_text("# hi\n")
    walked = _walk_workspace_sync(str(tmp_path))
    assert set(walked.keys()) == {"src/app.ts", "README.md"}
    # Hashes are populated, not empty strings.
    assert all(len(sha) == 64 for sha, _ in walked.values())


def test_walk_excludes_node_modules_and_friends(tmp_path: Path) -> None:
    """SNAPSHOT_EXCLUDES is the single source of truth for ignored dirs.
    Drift here is a real cost: snapshotting node_modules even once at scale
    inflates the blob store by GB."""
    keep = tmp_path / "src" / "app.ts"
    keep.parent.mkdir()
    keep.write_text("ok")

    for excluded in SNAPSHOT_EXCLUDES:
        d = tmp_path / excluded / "junk.bin"
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(b"\x00" * 16)

    walked = _walk_workspace_sync(str(tmp_path))
    assert set(walked.keys()) == {"src/app.ts"}


def test_walk_handles_nested_excludes(tmp_path: Path) -> None:
    """Excludes apply at any depth — `pkg/node_modules/...` is just as
    excluded as `node_modules/...` at the workspace root."""
    (tmp_path / "pkg" / "node_modules" / "foo").mkdir(parents=True)
    (tmp_path / "pkg" / "node_modules" / "foo" / "index.js").write_text("//")
    (tmp_path / "pkg" / "src.ts").write_text("ok")
    walked = _walk_workspace_sync(str(tmp_path))
    assert set(walked.keys()) == {"pkg/src.ts"}


# ─────────────────────────────────────────────────────────────────────────────
# Blob key sharding
# ─────────────────────────────────────────────────────────────────────────────

def test_blob_key_shards_by_first_two_chars() -> None:
    sha = "ab" + "0" * 62
    assert _blob_key(sha) == f"ab/{sha}"


def test_blob_key_is_deterministic_and_unique_per_sha() -> None:
    """A blob key collision = a content collision; sha256 makes that
    cryptographically negligible. Guard the function shape, not the hash."""
    a = _blob_key("ab" + "1" * 62)
    b = _blob_key("ab" + "2" * 62)
    assert a != b
    # Shard prefix shows up in the key path so object stores can balance.
    assert a.startswith("ab/")
    assert b.startswith("ab/")


# ─────────────────────────────────────────────────────────────────────────────
# Manifest equality (drives the per-turn no-op short-circuit)
# ─────────────────────────────────────────────────────────────────────────────

def test_walk_produces_deterministic_manifest(tmp_path: Path) -> None:
    """create_version no-ops when `new_manifest == parent.manifest`. That
    Python-level dict equality must reliably match on identical workspace
    contents, regardless of walk order. Two back-to-back walks of the same
    tree must produce equal manifests (same keys, same sha values).

    Why this matters: if `==` on the manifest were ever sensitive to
    insertion order (it isn't for Python dicts since 3.7, but worth
    pinning), every verify call in a debug loop would create a duplicate
    version and clutter the dropdown — exactly what the no-op exists to
    prevent.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("a")
    (tmp_path / "src" / "b.ts").write_text("b")
    (tmp_path / "README.md").write_text("ok")

    w1 = _walk_workspace_sync(str(tmp_path))
    w2 = _walk_workspace_sync(str(tmp_path))

    m1 = {p: sha for p, (sha, _) in w1.items()}
    m2 = {p: sha for p, (sha, _) in w2.items()}
    assert m1 == m2


def test_manifest_changes_when_a_single_file_changes(tmp_path: Path) -> None:
    """The other half of the no-op contract: any byte change to any file
    must break manifest equality, so create_version DOES insert a row.
    Catches a regression where the diff falls through and the rollback
    target is silently stale."""
    f = tmp_path / "app.ts"
    f.write_text("v1")
    before = {p: sha for p, (sha, _) in _walk_workspace_sync(str(tmp_path)).items()}

    f.write_text("v2")
    after = {p: sha for p, (sha, _) in _walk_workspace_sync(str(tmp_path)).items()}

    assert before != after
    assert before["app.ts"] != after["app.ts"]
