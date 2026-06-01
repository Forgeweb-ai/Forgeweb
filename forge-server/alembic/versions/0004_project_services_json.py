"""add services_json on projects

Holds the frontend/backend framework + port mapping that used to live in
forge.json::services. Storing it as TEXT keeps it shapeless from the DB's
perspective while letting the API layer round-trip JSON.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-27
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("services_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "services_json")
