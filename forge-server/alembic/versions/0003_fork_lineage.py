"""add forked_from_project_id + clone_count on projects

Lets `/api/projects/fork` track parentage (forked_from_project_id) and surface
"this project was cloned N times" on the source side (clone_count) without
joining the projects table to itself on every gallery render.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-27
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("forked_from_project_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_forked_from",
        "projects",
        "projects",
        ["forked_from_project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_projects_forked_from_project_id",
        "projects",
        ["forked_from_project_id"],
    )
    op.add_column(
        "projects",
        sa.Column("clone_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("projects", "clone_count")
    op.drop_index("ix_projects_forked_from_project_id", table_name="projects")
    op.drop_constraint("fk_projects_forked_from", "projects", type_="foreignkey")
    op.drop_column("projects", "forked_from_project_id")
