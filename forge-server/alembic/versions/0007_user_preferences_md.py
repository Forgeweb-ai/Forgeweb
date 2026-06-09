"""add preferences_md on users

Adds a single nullable TEXT column on `users` to store the user's free-form
preferences blob ("skills.md"). This blob is injected per-turn into every
project session's system prompt so the AI always remembers the user's
standing preferences (e.g. "always use Tailwind", "snake_case DB columns").

Why a dedicated column rather than a key inside user_settings.settings_json:
the blob is multi-KB, multi-line markdown — escaping it inside a JSON string
is awkward and forces a full read-modify-write of the whole settings blob on
every preference edit. A dedicated TEXT column lets us PUT it independently.

Per §3 token-cost rules: per-turn cost is `len(preferences_md)` tokens added
to the system prompt (cacheable, stable across turns of the same session).
Empty / NULL = zero added tokens. Predictable, bounded, flat across
conversation length.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("preferences_md", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "preferences_md")
