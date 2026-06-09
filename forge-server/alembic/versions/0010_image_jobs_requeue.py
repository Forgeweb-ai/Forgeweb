"""image_jobs: bounded re-queue for rate-limited jobs

Adds `attempts` and `next_attempt_at` so the worker can re-queue a job that
exhausted its in-flight rate-limit retries instead of permanently failing it
on the first sustained 429 from the provider.

Why this shape (not unbounded retry, not Redis):
  The previous flow held a worker-pool semaphore slot through every retry and
  permanently failed the job after the 3-attempt inner loop. At 100k+
  containers, an unbounded "retry until success" would starve the pool any
  time a provider entered backoff mode (× users × replicas). And a sustained
  429 isn't a job-level bug — it's a provider-side rate the user will recover
  from in seconds-to-minutes. So we re-queue: drop the slot, schedule a
  future re-attempt, let the worker drain it later on its normal poll. Cap
  total attempts at 10 so a broken provider can't ride the queue forever.

Index:
  Partial index on (status, next_attempt_at) WHERE status='queued' lets the
  claim query short-circuit to ready rows even when re-queued rows pile up.
  Partial keeps the index tiny — only queued rows live in it.

Backward-compat:
  Strictly additive. Existing rows get attempts=0, next_attempt_at=NULL
  (i.e. "ready immediately"), which the worker's claim query already treats
  as runnable. Downgrade drops the columns + the partial index.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-05
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision:      str                              = "0010"
down_revision: Union[str, None]                 = "0009"
branch_labels: Union[str, Sequence[str], None]  = None
depends_on:    Union[str, Sequence[str], None]  = None


def upgrade() -> None:
    # `attempts` is the number of times the worker has CLAIMED this job. It
    # increments on every re-queue (one inner 3-retry burst = one attempt).
    # Default 0 so existing rows are treated as "never attempted yet" — the
    # worker will pick them up exactly as it did before this migration.
    op.add_column(
        "image_jobs",
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # NULL = "ready immediately" (initial insert). Non-NULL = "do not claim
    # until t >= next_attempt_at". The worker's claim query filters on this.
    op.add_column(
        "image_jobs",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # Partial index over queued-only rows ordered by next_attempt_at so the
    # claim query can pick the next-due row in O(log n) regardless of how
    # many done/failed rows accumulate over a project's lifetime. NULL sorts
    # first under Postgres' default, which is what we want — never-deferred
    # rows are always due now.
    op.execute(
        "CREATE INDEX ix_image_jobs_queue_due "
        "ON image_jobs (next_attempt_at, created_at) "
        "WHERE status = 'queued'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_image_jobs_queue_due")
    op.drop_column("image_jobs", "next_attempt_at")
    op.drop_column("image_jobs", "attempts")
