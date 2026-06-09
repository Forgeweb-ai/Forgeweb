"""extend supabase_connections for provisioned-local schemas

Adds four nullable columns to `supabase_connections` so the same table can
represent BOTH external BYO Supabase rows (existing behaviour) AND
Forge-provisioned-local Postgres schemas (new in Phase A of the
Postgres-per-schema migration — see LAUNCH_PLAN §3b).

Columns:
  - provisioned_locally  BOOLEAN NOT NULL DEFAULT FALSE
        Disambiguator. FALSE keeps every existing row in BYO Supabase mode
        (current behaviour). TRUE means Forge created the schema + role
        inside its own local Postgres and may drop them on disconnect.
  - schema_name          VARCHAR(63) NULL
        Postgres identifier max is 63 chars. Format: app_<first8_of_uuid>.
  - role_name            VARCHAR(63) NULL
        Per-project Postgres role, same naming pattern as schema_name.
  - role_password_enc    TEXT NULL
        Fernet-encrypted password we wrote into the connection string for
        the runner container. Re-injected on every container boot. NULL for
        BYO Supabase rows (they carry anon_key + service_role_key instead).

Why nullable + default-false: this migration must not break the existing
BYO Supabase code paths. Every existing row is, by definition, a BYO row;
the default of FALSE preserves that meaning. New provisioned-local rows
written by /api/projects/{id}/db/provision set provisioned_locally=TRUE
and populate the other three columns.

Backward-compat: every consumer of SupabaseConnection currently reads only
supabase_url / anon_key / service_role_key. None of them reference the new
columns, so this is additive only — no downstream changes required to roll
out the schema. Phase B endpoints (/db/tables proxying to the schema)
*will* read the new columns; those land in a later migration's wake.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default="false" so existing rows get the safe default without a
    # data-migration step. Postgres rewrites the table once to fill the
    # column, but supabase_connections is tiny (<= N projects), so the lock
    # window is negligible.
    op.add_column(
        "supabase_connections",
        sa.Column(
            "provisioned_locally",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "supabase_connections",
        sa.Column("schema_name", sa.String(length=63), nullable=True),
    )
    op.add_column(
        "supabase_connections",
        sa.Column("role_name", sa.String(length=63), nullable=True),
    )
    op.add_column(
        "supabase_connections",
        sa.Column("role_password_enc", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Reverse order — drop the schema/role/password columns first because
    # they're referenced by application code that checks provisioned_locally
    # first. Safe either way; this just matches the upgrade order's logic.
    op.drop_column("supabase_connections", "role_password_enc")
    op.drop_column("supabase_connections", "role_name")
    op.drop_column("supabase_connections", "schema_name")
    op.drop_column("supabase_connections", "provisioned_locally")
