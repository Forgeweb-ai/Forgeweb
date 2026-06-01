"""add snapshots table, last_snapshot_at on projects, user_supabase_oauth table

These three together unlock:
  - Task 3/4 (snapshot + rehydrate): snapshots table + last_snapshot_at
  - Task 5 (Supabase OAuth BYOK):    user_supabase_oauth table

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-27
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── projects.last_snapshot_at ────────────────────────────────────────────
    op.add_column(
        "projects",
        sa.Column("last_snapshot_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── snapshots table ──────────────────────────────────────────────────────
    op.create_table(
        "snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_snapshots_project_id", "snapshots", ["project_id"])
    op.create_index(
        "ix_snapshots_project_created_desc",
        "snapshots",
        ["project_id", sa.text("created_at DESC")],
    )

    # ── user_supabase_oauth table ────────────────────────────────────────────
    op.create_table(
        "user_supabase_oauth",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supabase_user_email", sa.String(255), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_user_supabase_oauth_user_id", "user_supabase_oauth",
                    ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_supabase_oauth_user_id", table_name="user_supabase_oauth")
    op.drop_table("user_supabase_oauth")
    op.drop_index("ix_snapshots_project_created_desc", table_name="snapshots")
    op.drop_index("ix_snapshots_project_id", table_name="snapshots")
    op.drop_table("snapshots")
    op.drop_column("projects", "last_snapshot_at")
