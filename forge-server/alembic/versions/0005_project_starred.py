"""add starred_at on projects

Adds a single TIMESTAMPTZ column so a user can star/unstar a project.
Null = not starred, non-null = the time it was starred (used to sort the
Starred view by most-recently-starred).

A boolean would have worked but a timestamp gives us free ordering with no
extra column. Filtering is `starred_at IS NOT NULL` on the bounded per-user
project list — sub-linear, no index needed.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-01
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("starred_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "starred_at")
