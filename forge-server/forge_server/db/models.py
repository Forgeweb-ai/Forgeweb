"""
forge_server/db/models.py
==========================
ORM models for forge-server.

Schema lives in Postgres (local Supabase in dev, hosted Supabase in prod).
SQLite is no longer supported as a backend — the move to Postgres unlocks
RLS, realtime, native UUID, and TIMESTAMPTZ. See [[forge_storage_architecture]].

Conventions:
  - IDs: native PG UUID columns via `Uuid(as_uuid=False)`. The *column* is
    native UUID on disk (compact, properly indexed) but Python sees strings
    on read — so every existing consumer that does `project.id[:24]` or
    `f"forge-proj-{project.id}"` keeps working without edits.
  - Timestamps: TIMESTAMPTZ via `DateTime(timezone=True)`. Server-side defaults
    via `func.now()` so the DB clock is authoritative (no app-server skew).
  - Updated-at columns use both `server_default` and `onupdate` so INSERT and
    UPDATE both touch the column without app-layer bookkeeping.

Tables:
  users               — registered accounts
  user_settings       — per-user JSON config (design model, preferences)
  user_provider_keys  — per-user encrypted API keys for LLM providers
  projects            — one per user workspace
  dev_containers      — Docker container state per project
  supabase_connections — Supabase credentials per project (BYOK)
  project_env_vars    — per-project encrypted env vars (API keys, secrets, etc.)
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    CHAR, BigInteger, Boolean, Column, DateTime, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint, Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from forge_server.db.database import Base


def _uuid() -> str:
    """Default for id columns — returns the string form so Python-side stays
    string-typed even though the DB column is native UUID."""
    return str(uuid.uuid4())


# Reusable UUID column type. as_uuid=False means SQLAlchemy hands us back
# strings (preserving the existing Python interface across the codebase) while
# the DB column is still a real Postgres UUID. Best of both worlds.
UuidCol = Uuid(as_uuid=False)


# ─────────────────────────────────────────────────────────────────────────────
# User
# ─────────────────────────────────────────────────────────────────────────────

class User(Base):
    """Registered user. Auth via email + bcrypt password."""
    __tablename__ = "users"

    id               = Column(UuidCol, primary_key=True, default=_uuid)
    email            = Column(String(255), unique=True, nullable=False, index=True)
    username         = Column(String(100), unique=True, nullable=False)
    hashed_password  = Column(String(255), nullable=False)
    created_at       = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Auth state — new users start unverified, onboarding incomplete.
    # Existing rows migrated from SQLite are backfilled to verified=True,
    # onboarding_completed=True by the migration script.
    email_verified        = Column(Boolean, default=False, nullable=False)
    onboarding_completed  = Column(Boolean, default=False, nullable=False)

    # Onboarding-captured profile fields
    full_name     = Column(String(255), nullable=True)
    role          = Column(String(50),  nullable=True)   # founder|product|designer|engineer|consultant|marketing-sales|operations|other
    company_size  = Column(String(20),  nullable=True)   # solo|2-20|21-200|200+
    theme_pref    = Column(String(10),  nullable=True)   # light|dark

    # Free-form preferences markdown ("skills.md") — injected into every
    # project session's system prompt per-turn so the AI remembers user
    # standing preferences across all of their apps. Edited via Settings →
    # Preferences. Null/empty = nothing injected (zero added tokens).
    # Stored as TEXT (not embedded in user_settings.settings_json) because
    # it's a multi-KB blob and JSON escaping multi-line markdown is awkward.
    # See migration 0007.
    preferences_md = Column(Text, nullable=True)

    projects      = relationship("Project",         back_populates="user", cascade="all, delete-orphan")
    settings      = relationship("UserSettings",    back_populates="user", uselist=False, cascade="all, delete-orphan")
    provider_keys = relationship("UserProviderKey", back_populates="user", cascade="all, delete-orphan")


# ─────────────────────────────────────────────────────────────────────────────
# Project
# ─────────────────────────────────────────────────────────────────────────────

class Project(Base):
    """
    A webapp project. Maps 1-to-1 with an OpenCode workspace directory.

    workspace_path:  absolute path on the server shared with opencode
                     e.g. /forge-data/users/{user_id}/projects/{id}/workspace
    """
    __tablename__ = "projects"

    id               = Column(UuidCol, primary_key=True, default=_uuid)
    user_id          = Column(UuidCol, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name             = Column(String(255), nullable=False)
    description      = Column(Text, default="")
    workspace_path   = Column(String(500), nullable=False)
    created_at       = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at       = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # opencode session to resume on project open
    opencode_session_id = Column(String(255), nullable=True)
    # Stack type detected/set by AI — e.g. "react-fastapi", "nextjs", "react-express"
    stack               = Column(String(100), nullable=True)
    # Services block — frontend / backend framework + port, formerly in forge.json.
    # Stored as JSON text so the API layer can round-trip it without a schema
    # commitment to the shape (frameworks come and go).
    services_json       = Column(Text, nullable=True)
    # Showcase: when set, this project appears in the home-page showcase grid
    showcased_at        = Column(DateTime(timezone=True), nullable=True)
    showcase_name       = Column(String(255), nullable=True)
    showcase_description = Column(Text, nullable=True)
    # Path to screenshot PNG captured at showcase time
    thumbnail_url       = Column(String(500), nullable=True)

    # When the most-recent successful snapshot was uploaded. Used by the
    # background snapshot worker to decide "has this project changed since
    # the last snapshot?" without joining the snapshots table on every scan.
    last_snapshot_at    = Column(DateTime(timezone=True), nullable=True)

    # Starred state — null = not starred, timestamp = when the user starred it.
    # Kept as a TIMESTAMPTZ (not a Boolean) so we can sort starred lists by
    # "most recently starred" without a second column. Filter is
    # `starred_at IS NOT NULL` — sub-linear on the bounded per-user project
    # list (<1k rows per user even at scale), no index needed.
    starred_at          = Column(DateTime(timezone=True), nullable=True)

    # Fork lineage. If this project was created via /api/projects/fork from
    # another project (typically a showcased template), forked_from_project_id
    # points at the source. ON DELETE SET NULL — deleting the source shouldn't
    # cascade-delete every fork ever made of it. clone_count is the
    # denormalised count on the SOURCE side so we can show "Cloned 47 times"
    # without joining; it's bumped atomically by the fork endpoint.
    forked_from_project_id = Column(UuidCol, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    clone_count            = Column(Integer, nullable=False, server_default="0")

    # Current materialized version. NULL until the first version is created
    # (new projects don't have a version row until the first AI turn lands).
    # Rollback sets this to an ancestor; descendants are then marked
    # orphaned. ON DELETE SET NULL so a stray version delete cannot
    # cascade-nuke the project. See [[forge_storage_architecture]].
    head_version_id = Column(UuidCol, ForeignKey("versions.id", ondelete="SET NULL"), nullable=True)

    user      = relationship("User", back_populates="projects")
    container = relationship("DevContainer",        back_populates="project", uselist=False, cascade="all, delete-orphan")
    supabase  = relationship("SupabaseConnection",  back_populates="project", uselist=False, cascade="all, delete-orphan")
    env_vars  = relationship("ProjectEnvVar",       back_populates="project", cascade="all, delete-orphan")
    snapshots = relationship("Snapshot",            back_populates="project", cascade="all, delete-orphan")
    # Versions: per-AI-turn content-addressed snapshots. Cascade-delete so
    # tearing down a project also drops its version chain (and the blob
    # refcounts are decremented by the deletion hook in storage/versions.py).
    versions  = relationship(
        "Version",
        back_populates="project",
        cascade="all, delete-orphan",
        foreign_keys="Version.project_id",
    )


# ─────────────────────────────────────────────────────────────────────────────
# DevContainer
# ─────────────────────────────────────────────────────────────────────────────

class DevContainer(Base):
    """
    Tracks the Docker container state for one project's dev server.

    Status lifecycle:
      none → creating → installing → running → sleeping → running → ...
                                                         → stopped (manual)
                                                         → crashed
    """
    __tablename__ = "dev_containers"

    id             = Column(UuidCol, primary_key=True, default=_uuid)
    project_id     = Column(UuidCol, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    # Docker container ID (set after docker create)
    container_id   = Column(String(100), nullable=True)
    # Container name — forge-proj-{project_id[:12]}
    container_name = Column(String(120), nullable=True)

    # Status: creating | installing | running | sleeping | stopped | crashed
    status         = Column(String(20), default="stopped", nullable=False)

    # Internal port (always container_dev_port = 3000)
    internal_port  = Column(Integer, default=3000)

    # Preview URL — subdomain or path-based depending on config
    preview_url    = Column(String(500), nullable=True)

    # Last ping from forge-ui (used by sleep worker)
    last_ping_at   = Column(DateTime(timezone=True), nullable=True)

    started_at     = Column(DateTime(timezone=True), nullable=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at     = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    project = relationship("Project", back_populates="container")


# ─────────────────────────────────────────────────────────────────────────────
# SupabaseConnection
# ─────────────────────────────────────────────────────────────────────────────

class SupabaseConnection(Base):
    """
    Postgres connection for a Forge project. Two flavours, same table:

      1. **Provisioned locally** (`provisioned_locally=True`): Forge created a
         schema + role inside its own local Postgres for the user's generated
         app. This is the default in `local-self-host` mode — the user doesn't
         set up a second Supabase. `supabase_url` points at Forge's local
         Postgres host, `anon_key` carries the scoped role's connection-string
         password, `service_role_key` is NULL (no privileged key needed; the
         role is already scoped to one schema).

      2. **BYO Supabase** (`provisioned_locally=False`): the user connected an
         external Supabase project via OAuth or manual paste. `supabase_url`
         is `https://<ref>.supabase.co`, `anon_key` / `service_role_key` are
         the real Supabase JWTs. Used in `hosted` mode and as the migration
         target when a `local-self-host` user goes to production.

    Why one table for both: `/db/info` and every consumer just needs a
    connection-string-equivalent. Splitting would force every reader to JOIN
    or UNION two tables for no semantic gain. Disambiguate via the flag.
    """
    __tablename__ = "supabase_connections"

    id               = Column(UuidCol, primary_key=True, default=_uuid)
    project_id       = Column(UuidCol, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    supabase_url     = Column(String(500), nullable=False)
    anon_key         = Column(Text, nullable=False)
    service_role_key = Column(Text, nullable=True)   # Fernet-encrypted at rest

    # ── Provisioned-locally fields (NULL when this is a BYO Supabase row) ────
    # True ⇒ Forge owns this schema/role inside its local Postgres and may
    # drop them on disconnect; False ⇒ external Supabase, never touch.
    provisioned_locally = Column(Boolean, nullable=False, server_default="false")
    # Postgres schema name, e.g. "app_a1b2c3d4". NULL for BYO Supabase rows.
    schema_name         = Column(String(63), nullable=True)
    # Postgres role name, same shape. NULL for BYO.
    role_name           = Column(String(63), nullable=True)
    # Fernet-encrypted role password (the password we wrote into the connstr).
    # NULL for BYO rows. We keep it so we can hand the connstr to the runner
    # container every boot — we never let the role be passwordless.
    role_password_enc   = Column(Text, nullable=True)

    connected_at     = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at     = Column(DateTime(timezone=True), nullable=True)

    project = relationship("Project", back_populates="supabase")


# ─────────────────────────────────────────────────────────────────────────────
# ProjectEnvVar
# ─────────────────────────────────────────────────────────────────────────────

class ProjectEnvVar(Base):
    """
    Per-project environment variable, stored encrypted.

    Values are Fernet-encrypted at rest (same key as SupabaseConnection).
    Injected into:
      1. The workspace .env / .env.local at container start
      2. The container environment via Docker --env flags

    forge.json on disk only lists key names (masked) — never values.
    This table is the single source of truth for secret values.
    """
    __tablename__ = "project_env_vars"

    id             = Column(UuidCol, primary_key=True, default=_uuid)
    project_id     = Column(UuidCol, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    # The env var name, e.g. "OPENAI_API_KEY"
    key            = Column(String(255), nullable=False)
    # Fernet-encrypted value
    value_enc      = Column(Text, nullable=False)
    # Optional human label shown in UI, e.g. "OpenAI production key"
    label          = Column(String(255), nullable=True)
    # Whether to inject into container at runtime (True) or only into .env file (False)
    inject_runtime = Column(Boolean, default=True, nullable=False)

    created_at     = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at     = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_project_env_key"),
    )

    project = relationship("Project", back_populates="env_vars")


# ─────────────────────────────────────────────────────────────────────────────
# UserSettings
# ─────────────────────────────────────────────────────────────────────────────

class UserSettings(Base):
    """
    Per-user configuration stored as a JSON blob.

    Current keys (all optional — defaults applied in API layer):
      primary_model — model ID for the main coding / chat agent
                      e.g. "anthropic/claude-sonnet-4-6", "opencode/deepseek-v4-flash-free"
      design_model  — model ID for design-analyst + design-critic subagents
                      e.g. "anthropic/claude-sonnet-4-6" or "moonshot/kimi-k2-0905-preview"

    Defaults (SETTINGS_DEFAULTS in api/constants.py) seed both fields to the
    free DeepSeek tier so a fresh signup is usable without a provider key.

    Stored as TEXT (not JSONB) for now to stay compatible with the existing
    API layer that calls json.loads/dumps. Migrate to JSONB later if we ever
    want to query inside the blob.
    """
    __tablename__ = "user_settings"

    id            = Column(UuidCol, primary_key=True, default=_uuid)
    user_id       = Column(UuidCol, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    settings_json = Column(Text, default="{}", nullable=False)
    updated_at    = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="settings")


# ─────────────────────────────────────────────────────────────────────────────
# UserProviderKey
# ─────────────────────────────────────────────────────────────────────────────

class UserProviderKey(Base):
    """
    Per-user API key for an LLM provider, stored Fernet-encrypted.

    provider_id  — opencode provider id, e.g. "anthropic", "moonshot", "openai"
    key_enc      — Fernet-encrypted API key value
    label        — optional human label, e.g. "Personal Anthropic key"

    On save the API layer also writes the decrypted key to opencode's auth.json
    so opencode picks it up immediately without restart.

    This is the BYOK-LLM storage layer for [[forge_v1_scope]].
    """
    __tablename__ = "user_provider_keys"

    id          = Column(UuidCol, primary_key=True, default=_uuid)
    user_id     = Column(UuidCol, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider_id = Column(String(100), nullable=False)
    key_enc     = Column(Text, nullable=False)
    label       = Column(String(255), nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at  = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "provider_id", name="uq_user_provider_key"),
    )

    user = relationship("User", back_populates="provider_keys")


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot
# ─────────────────────────────────────────────────────────────────────────────

class Snapshot(Base):
    """
    A snapshot of a project's workspace, stored as a .tar.zst in Supabase
    Storage. The disk workspace is the hot copy; snapshots are the durability
    + portability + sharing layer. See [[forge-storage-architecture]].

    storage_key format: "{user_id}/{project_id}/{timestamp}.tar.zst"
    """
    __tablename__ = "snapshots"

    id          = Column(UuidCol, primary_key=True, default=_uuid)
    project_id  = Column(UuidCol, ForeignKey("projects.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    storage_key = Column(String(500), nullable=False)
    size_bytes  = Column(BigInteger, nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now(),
                         nullable=False)

    project = relationship("Project", back_populates="snapshots")


# ─────────────────────────────────────────────────────────────────────────────
# Blob + Version — content-addressed snapshot store
# ─────────────────────────────────────────────────────────────────────────────
#
# Replaces the per-snapshot tarball model with per-file deduplication:
# each unique file content (by sha256) is stored exactly once in Supabase
# Storage and shared by all versions of all projects that reference it.
# A Version is the project's state at a point in time, encoded as
# manifest = { rel_path: sha256 }. See [[forge_storage_architecture]].

class Blob(Base):
    """One row per unique file CONTENT across the platform.

    refcount is maintained transactionally on version create/orphan/hard-
    delete. When refcount hits 0, the GC worker is free to delete the
    backing Storage object (after a grace window — see versions.py).
    """
    __tablename__ = "blobs"

    sha256       = Column(CHAR(64), primary_key=True)
    size_bytes   = Column(BigInteger, nullable=False)
    content_type = Column(String(100), nullable=True)
    # Number of live manifest entries pointing at this blob. NEVER decrement
    # without holding the version's transaction — the increment + decrement
    # paths must mirror each other or storage and DB drift apart.
    refcount     = Column(Integer, nullable=False, server_default="0")
    created_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Version(Base):
    """One row per AI turn (created post-verify).

    manifest is a flat dict `{ rel_path: sha256 }` covering every file in
    the workspace at the moment the version was captured (excluding
    node_modules, .git, dist, etc. — see SNAPSHOT_EXCLUDES).

    Live versions have orphaned_at = NULL. A soft rollback to an ancestor
    marks intervening descendants orphaned but does NOT delete them; the
    GC worker hard-deletes (and decrements refcounts) after the grace
    window. parent_version_id forms a chain — linear in v1, branchable
    later for free.
    """
    __tablename__ = "versions"

    id                = Column(UuidCol, primary_key=True, default=_uuid)
    project_id        = Column(UuidCol, ForeignKey("projects.id", ondelete="CASCADE"),
                               nullable=False)
    parent_version_id = Column(UuidCol, ForeignKey("versions.id", ondelete="SET NULL"),
                               nullable=True)
    prompt            = Column(Text, nullable=True)   # the user prompt that produced this version
    summary           = Column(Text, nullable=True)   # short label for the dropdown (AI-generated)
    manifest          = Column(JSONB, nullable=False)
    orphaned_at       = Column(DateTime(timezone=True), nullable=True)
    created_at        = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    project = relationship("Project", back_populates="versions", foreign_keys=[project_id])
    parent  = relationship("Version", remote_side=[id])


# ─────────────────────────────────────────────────────────────────────────────
# UserSupabaseOAuth — per-user Supabase OAuth token (BYOK for user apps)
# ─────────────────────────────────────────────────────────────────────────────

class UserSupabaseOAuth(Base):
    """
    Stores the OAuth tokens granted by a user's Supabase account so Forge can
    use the Management API to provision projects on their behalf. The user
    remains the data owner — these are scoped delegation tokens, not their
    service role keys. See [[forge_generated_app_data]].
    """
    __tablename__ = "user_supabase_oauth"

    id                  = Column(UuidCol, primary_key=True, default=_uuid)
    user_id             = Column(UuidCol, ForeignKey("users.id", ondelete="CASCADE"),
                                 nullable=False, unique=True, index=True)
    # Fernet-encrypted at rest (same key as other encrypted cols).
    access_token_enc    = Column(Text, nullable=False)
    refresh_token_enc   = Column(Text, nullable=True)
    expires_at          = Column(DateTime(timezone=True), nullable=True)
    # The Supabase account email — handy to display in the UI ("Connected as foo@bar")
    supabase_user_email = Column(String(255), nullable=True)
    connected_at        = Column(DateTime(timezone=True),
                                 server_default=func.now(), nullable=False)


# ─────────────────────────────────────────────────────────────────────────────
# ImageJob — async image-generation queue
# ─────────────────────────────────────────────────────────────────────────────

class ImageJob(Base):
    """
    One row per image the main agent has requested.

    Flow (see forge_server/imagegen/worker.py + TODO_IMAGE_GEN.md):
      1. opencode's `request_images` tool POSTs to forge-server, which
         inserts N rows with status='queued' and returns the slot_ids.
      2. The worker drains queued rows, calls the provider with the user's
         decrypted BYOK key, uploads the result, sets output_url + status='done'.
      3. The runner resolves /forge-img/{slot_id} by looking up the row:
         done → 302 to output_url; queued/running → shimmer SVG;
         failed → fallback URL (alt-keyword Unsplash) so the user's page
         is never broken just because one provider call hiccuped.

    Why a table, not a queue (Redis/RabbitMQ): we already require Postgres,
    and image-gen volume is bounded (max 6 per turn, hundreds per project
    lifetime). A table + indexed SELECT FOR UPDATE SKIP LOCKED gives us a
    free queue with durability, dedup, and history — no new infra to operate
    × 100k containers. Revisit only if a single project ever queues 1M jobs.

    Indexing:
      - (project_id, status) — worker scan + status pill query
      - (project_id, slot_id) UNIQUE — runner /forge-img/{slot_id} lookup
      - (dedup_hash) UNIQUE WHERE dedup_hash IS NOT NULL — fast "same prompt
        already generated?" check; partial index keeps it sub-linear without
        bloating on rows that opted out of dedup.

    `slot_id` is a short opaque string (12 random base32 chars) the main
    agent embeds in the JSX (`<img src="/forge-img/{slot_id}.png">`). It's
    project-scoped, not globally unique, so leaking one across projects
    can't even be used to enumerate other projects' assets.
    """
    __tablename__ = "image_jobs"

    id           = Column(UuidCol, primary_key=True, default=_uuid)
    project_id   = Column(UuidCol, ForeignKey("projects.id", ondelete="CASCADE"),
                          nullable=False)
    # Short opaque slot id embedded in generated JSX. Project-scoped uniqueness.
    slot_id      = Column(String(24), nullable=False)

    # queued | running | done | failed
    # State machine is one-way: queued → running → (done | failed).
    # Failed jobs are NOT retried by the worker automatically — the user can
    # re-trigger from the FE if they want a second attempt. Auto-retry on a
    # provider that just charged us is a cost-surprise bug at 100k scale.
    status       = Column(String(16), nullable=False, server_default="queued")

    provider_id  = Column(String(64),  nullable=False)
    model_id     = Column(String(128), nullable=False)
    prompt       = Column(Text, nullable=False)

    # Reference image, when this is an img-to-img request. References the
    # content-addressed blob store ([[forge_versioning_v1]]) by sha256 so
    # the same reference doesn't cost extra storage. NULL = pure text-to-image.
    ref_blob_sha = Column(CHAR(64), nullable=True)

    # Target size, e.g. "1024x1024". Echo of the registry's size — kept
    # denormalised so reading a finished job doesn't require re-resolving
    # the registry version it was generated against.
    size         = Column(String(16), nullable=False)

    # sha256(f"{provider}|{model}|{size}|{prompt}|{ref_blob_sha or ''}")
    # NULL when the caller opted out of dedup (e.g. a re-roll button). The
    # UNIQUE WHERE NOT NULL index is added in the alembic migration, not
    # here, because SQLAlchemy's UniqueConstraint doesn't support partial
    # indexes portably.
    dedup_hash   = Column(CHAR(64), nullable=True)

    # Final asset URL on success. Public Supabase Storage URL when the user
    # has Supabase connected; otherwise the runner-served `/forge-img/...`
    # path that wraps the on-disk file.
    output_url   = Column(String(1024), nullable=True)

    # Short user-visible failure reason. NEVER carries provider raw output
    # (could leak the key or internal IDs); the worker maps provider errors
    # to a small set of categories: "rate_limit", "auth", "content_policy",
    # "timeout", "unknown".
    error        = Column(String(64), nullable=True)

    created_at   = Column(DateTime(timezone=True),
                          server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Number of times the worker has claimed this job. Increments on every
    # re-queue (one inner 3-retry burst = one attempt). Capped by the worker
    # at MAX_ATTEMPTS — beyond that the job is permanently failed so a
    # misbehaving provider can't ride the queue forever.
    attempts        = Column(Integer, nullable=False, server_default="0")

    # NULL = "ready immediately". Non-NULL = "worker must not claim until
    # t >= next_attempt_at". Set by the worker when re-queueing after a
    # rate-limit burst exhausted the inner retries.
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("project_id", "slot_id", name="uq_image_job_project_slot"),
        Index("ix_image_jobs_project_status", "project_id", "status"),
        # Partial index over queued-only rows, ordered by due-time. Lets the
        # claim query find the next-due row in O(log n) regardless of how
        # many done/failed rows accumulate over a project's lifetime. The
        # partial-WHERE is added in alembic (SQLAlchemy can't express it
        # portably); declared here for documentation only.
    )
