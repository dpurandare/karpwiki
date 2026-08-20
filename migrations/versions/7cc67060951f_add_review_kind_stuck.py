"""add review_kind.stuck

Revision ID: 7cc67060951f
Revises: 7eb53cee0b95
Create Date: 2026-08-20 00:00:00.000000
"""

from alembic import op


revision = '7cc67060951f'
down_revision = '7eb53cee0b95'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Stuck-Pipeline Sweep Detector (phase3-tasklist.md step 64) — a new `review_item.kind`
    # for a source parked in `submitted`/`classified`/`ingesting` past
    # `KARPWIKI_STUCK_PIPELINE_THRESHOLD_HOURS`, resolved via `retry`/`abort`/`dismiss`.
    # PG12+ allows ADD VALUE outside an explicit transaction block as long as the new value
    # isn't used in the same one — true here, nothing below reads it.
    op.execute("ALTER TYPE review_kind ADD VALUE IF NOT EXISTS 'stuck'")


def downgrade() -> None:
    # Postgres has no DROP VALUE — same as every other enum-value migration would face;
    # nothing to reverse without rebuilding the type.
    pass
