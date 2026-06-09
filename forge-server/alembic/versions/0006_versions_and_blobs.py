"""content-addressed blob store + per-AI-turn versions

Replaces the per-snapshot full-tarball model (legacy `snapshots` table, kept
intact for any old rows) with a deduplicated content-addressed store + a
linear version chain per project.

Tables:
  blobs(sha256 PK, size_bytes, content_type, refcount, created_at)
    - One row per unique file CONTENT across all projects, all users.
    - refcount is the number of manifest entries pointing at this blob;
      it is maintained transactionally on version create/orphan/delete.
      refcount = 0 means the GC worker can drop the Storage object.
    - The blob bytes themselves live in Supabase Storage at
      `blobs/<first2>/<sha256>` (the 2-char prefix shards 256 ways so a
      single bucket "directory" doesn't accumulate millions of objects).

  versions(id PK, project_id, parent_version_id, prompt, summary,
           manifest jsonb, orphaned_at, created_at)
    - One row per AI turn (post-verify). The user-visible "version" of a
      project at a moment in time.
    - manifest is `{ "<rel-path>": "<sha256>" }`. Stored inline as jsonb
      (atomic with the row insert; ~50–500KB for typical projects; if
      avg manifest size crosses ~1MB this should move to Supabase Storage).
    - parent_version_id is a self-FK forming a linear chain (per project)
      today, but the column shape supports branching for free later.
    - orphaned_at = NULL → live version (shown in UI).
      orphaned_at = <ts>  → soft-deleted by a rollback that targeted an
      earlier ancestor. The row + manifest stick around until the GC
      grace window (e.g. 7 days) elapses, then the version is hard-
      deleted and refcounts decremented.

  projects.head_version_id → versions(id)
    - The version currently materialized in the workspace dir. Rollback
      sets this to an ancestor and orphans the descendants.
    - ON DELETE SET NULL: deleting a version (GC) on a project whose head
      points at it should null the head, not cascade-delete the project.
      In practice this only happens during destructive truncation; the
      caller is expected to set head explicitly before allowing it.

Index strategy:
  versions(project_id, created_at DESC) — drives the dropdown list query.
  partial index versions(project_id) WHERE orphaned_at IS NULL — the
    common "what versions does the user see right now?" filter.
  blobs PK already covers the dedup lookup (`SELECT 1 FROM blobs WHERE
    sha256 = $1`); refcount is a hot column for GC, but at v1 scale a
    partial index on `WHERE refcount = 0` is unnecessary — sequential
    scan over the small free-list is fine until blobs > ~1M rows.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-02
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── blobs ────────────────────────────────────────────────────────────────
    # sha256 is a 64-char hex string. We use CHAR(64) over VARCHAR(64) for the
    # fixed-width predictability — no length-byte overhead, identical compare
    # cost in Postgres, and it documents the invariant in the schema.
    op.create_table(
        "blobs",
        sa.Column("sha256",       sa.CHAR(64),    primary_key=True),
        sa.Column("size_bytes",   sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(100),  nullable=True),
        sa.Column("refcount",     sa.Integer(),    nullable=False, server_default="0"),
        sa.Column("created_at",   sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )

    # ── versions ─────────────────────────────────────────────────────────────
    op.create_table(
        "versions",
        sa.Column("id",                sa.Uuid(), primary_key=True),
        sa.Column("project_id",        sa.Uuid(), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(), nullable=True),
        sa.Column("prompt",            sa.Text(), nullable=True),
        sa.Column("summary",           sa.Text(), nullable=True),
        sa.Column("manifest",          JSONB(),  nullable=False),
        sa.Column("orphaned_at",       sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",        sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"],        ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_version_id"], ["versions.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_versions_project_created_desc",
        "versions",
        ["project_id", sa.text("created_at DESC")],
    )
    # Partial index: the common UI query is "give me the live (non-orphaned)
    # versions for project X". Postgres uses this for the list-versions
    # endpoint without scanning orphaned rows.
    op.create_index(
        "ix_versions_project_live",
        "versions",
        ["project_id"],
        postgresql_where=sa.text("orphaned_at IS NULL"),
    )

    # ── projects.head_version_id ─────────────────────────────────────────────
    # Added AFTER versions exists so the FK is satisfiable. SET NULL on delete
    # so a stray version-delete doesn't cascade into the project.
    op.add_column(
        "projects",
        sa.Column("head_version_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_head_version",
        "projects",
        "versions",
        ["head_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_projects_head_version", "projects", type_="foreignkey")
    op.drop_column("projects", "head_version_id")
    op.drop_index("ix_versions_project_live", table_name="versions")
    op.drop_index("ix_versions_project_created_desc", table_name="versions")
    op.drop_table("versions")
    op.drop_table("blobs")
