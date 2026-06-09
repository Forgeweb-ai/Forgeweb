"""image_jobs queue for AI image generation

Adds a single table that backs the async image-gen feature (see
TODO_IMAGE_GEN.md). Each row is one image the main agent requested via the
opencode `request_images` tool; a background worker drains queued rows,
calls the user's BYOK image provider, and stores the result URL.

Why a table (not Redis / a real queue):
  We already require Postgres. Image-gen volume is bounded (max 6 per turn,
  hundreds per project lifetime). A table + SELECT FOR UPDATE SKIP LOCKED
  gives us durability, dedup, and history for free. Adding a queue service
  to operate × 100k containers is the wrong direction at this scale.

Indexes:
  - `ix_image_jobs_project_status` — worker scan and status-pill query.
    Composite (project_id, status) so the cheap "any pending images for
    project X?" lookup is sub-linear regardless of project history depth.
  - `uq_image_job_project_slot` — runner /forge-img/{slot_id} resolver.
    Slot id is project-scoped so a leak can't enumerate other projects.
  - `ix_image_jobs_dedup_partial` — partial unique on dedup_hash WHERE NOT
    NULL. NULL hashes (re-rolls / opted out of dedup) skip the constraint;
    everything else short-circuits "same prompt twice" without a full table
    scan.

Backward-compat:
  Strictly additive. No existing reader of any other table sees this
  unless they explicitly query it.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-04
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision:      str                              = "0009"
down_revision: Union[str, None]                 = "0008"
branch_labels: Union[str, Sequence[str], None]  = None
depends_on:    Union[str, Sequence[str], None]  = None


def upgrade() -> None:
    op.create_table(
        "image_jobs",
        sa.Column("id",           sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slot_id",      sa.String(length=24),  nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("provider_id",  sa.String(length=64),  nullable=False),
        sa.Column("model_id",     sa.String(length=128), nullable=False),
        sa.Column("prompt",       sa.Text(),             nullable=False),
        sa.Column("ref_blob_sha", sa.CHAR(length=64),    nullable=True),
        sa.Column("size",         sa.String(length=16),  nullable=False),
        sa.Column("dedup_hash",   sa.CHAR(length=64),    nullable=True),
        sa.Column("output_url",   sa.String(length=1024), nullable=True),
        sa.Column("error",        sa.String(length=64),  nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("project_id", "slot_id", name="uq_image_job_project_slot"),
    )

    op.create_index(
        "ix_image_jobs_project_status",
        "image_jobs",
        ["project_id", "status"],
    )

    # Partial unique index on dedup_hash. Postgres-only syntax — we are
    # Postgres-only since the SQLite drop ([[forge_storage_architecture]]).
    # Doing this via `op.execute` rather than `op.create_index(..., postgresql_where=)`
    # because the latter's syntax has been brittle across alembic versions and
    # this is one line we'd rather read at a glance during incident review.
    op.execute(
        "CREATE UNIQUE INDEX ix_image_jobs_dedup_partial "
        "ON image_jobs (dedup_hash) WHERE dedup_hash IS NOT NULL"
    )


def downgrade() -> None:
    # Drop in reverse-dependency order (indexes/constraints before the table).
    op.execute("DROP INDEX IF EXISTS ix_image_jobs_dedup_partial")
    op.drop_index("ix_image_jobs_project_status", table_name="image_jobs")
    op.drop_table("image_jobs")
