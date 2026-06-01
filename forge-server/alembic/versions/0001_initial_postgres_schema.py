"""initial postgres schema

Initial migration: creates every table from forge_server.db.models with the
new Postgres-native types (UUID, TIMESTAMPTZ, server-side defaults).

This is the first migration after moving off SQLite — see
[[forge_storage_architecture]]. For copying data from the old forge.db, run
`python scripts/migrate_sqlite_to_postgres.py` *after* this migration applies.

Revision ID: 0001
Revises:
Create Date: 2026-05-27
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── users ────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("onboarding_completed", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("role", sa.String(50), nullable=True),
        sa.Column("company_size", sa.String(20), nullable=True),
        sa.Column("theme_pref", sa.String(10), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ── projects ─────────────────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("workspace_path", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("opencode_session_id", sa.String(255), nullable=True),
        sa.Column("stack", sa.String(100), nullable=True),
        sa.Column("showcased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("showcase_name", sa.String(255), nullable=True),
        sa.Column("showcase_description", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.String(500), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])

    # ── dev_containers ───────────────────────────────────────────────────────
    op.create_table(
        "dev_containers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("container_id", sa.String(100), nullable=True),
        sa.Column("container_name", sa.String(120), nullable=True),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'stopped'")),
        sa.Column("internal_port", sa.Integer(), nullable=True,
                  server_default=sa.text("3000")),
        sa.Column("preview_url", sa.String(500), nullable=True),
        sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_dev_containers_project_id", "dev_containers",
                    ["project_id"], unique=True)

    # ── supabase_connections ─────────────────────────────────────────────────
    op.create_table(
        "supabase_connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("supabase_url", sa.String(500), nullable=False),
        sa.Column("anon_key", sa.Text(), nullable=False),
        sa.Column("service_role_key", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_supabase_connections_project_id",
                    "supabase_connections", ["project_id"], unique=True)

    # ── project_env_vars ─────────────────────────────────────────────────────
    op.create_table(
        "project_env_vars",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value_enc", sa.Text(), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("inject_runtime", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "key", name="uq_project_env_key"),
    )
    op.create_index("ix_project_env_vars_project_id", "project_env_vars",
                    ["project_id"])

    # ── user_settings ────────────────────────────────────────────────────────
    op.create_table(
        "user_settings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_user_settings_user_id", "user_settings",
                    ["user_id"], unique=True)

    # ── user_provider_keys ───────────────────────────────────────────────────
    op.create_table(
        "user_provider_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.String(100), nullable=False),
        sa.Column("key_enc", sa.Text(), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "provider_id", name="uq_user_provider_key"),
    )
    op.create_index("ix_user_provider_keys_user_id", "user_provider_keys",
                    ["user_id"])


def downgrade() -> None:
    # Drop in reverse-dependency order so foreign keys don't block.
    op.drop_index("ix_user_provider_keys_user_id", table_name="user_provider_keys")
    op.drop_table("user_provider_keys")
    op.drop_index("ix_user_settings_user_id", table_name="user_settings")
    op.drop_table("user_settings")
    op.drop_index("ix_project_env_vars_project_id", table_name="project_env_vars")
    op.drop_table("project_env_vars")
    op.drop_index("ix_supabase_connections_project_id", table_name="supabase_connections")
    op.drop_table("supabase_connections")
    op.drop_index("ix_dev_containers_project_id", table_name="dev_containers")
    op.drop_table("dev_containers")
    op.drop_index("ix_projects_user_id", table_name="projects")
    op.drop_table("projects")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
