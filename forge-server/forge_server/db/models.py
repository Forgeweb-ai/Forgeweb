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
    BigInteger, Boolean, Column, DateTime, ForeignKey,
    Integer, String, Text, UniqueConstraint, Uuid,
)
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

    user      = relationship("User", back_populates="projects")
    container = relationship("DevContainer",        back_populates="project", uselist=False, cascade="all, delete-orphan")
    supabase  = relationship("SupabaseConnection",  back_populates="project", uselist=False, cascade="all, delete-orphan")
    env_vars  = relationship("ProjectEnvVar",       back_populates="project", cascade="all, delete-orphan")
    snapshots = relationship("Snapshot",            back_populates="project", cascade="all, delete-orphan")


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

    # Last ping from forge-ui-new (used by sleep worker)
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
    Supabase project credentials for a forge project.
    service_role_key is stored encrypted (see supabase_manager.py).

    Future columns to add when wiring OAuth (Task #5):
      - management_token_enc  (Supabase Management API token, scoped)
      - supabase_org_id       (org the project lives in)
      - project_ref           (Supabase project ref, for Management API calls)
    """
    __tablename__ = "supabase_connections"

    id               = Column(UuidCol, primary_key=True, default=_uuid)
    project_id       = Column(UuidCol, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    supabase_url     = Column(String(500), nullable=False)
    anon_key         = Column(Text, nullable=False)
    service_role_key = Column(Text, nullable=True)   # Fernet-encrypted at rest

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
