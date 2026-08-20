"""add review_kind.pii_review

Revision ID: f27a7aa73ce2
Revises: e8b1f5be5c9c
Create Date: 2026-08-20 00:00:00.000000
"""

from alembic import op


revision = 'f27a7aa73ce2'
down_revision = 'e8b1f5be5c9c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PII detection at ingestion (07 §2, phase3-tasklist.md step 71) — a new
    # `review_item.kind` for a source the dedicated scanner blocked before classification
    # ran, resolved via `acknowledge`/`reject`. Same shape as `ReviewKind.stuck`'s own
    # migration (step 64) — PG12+ allows ADD VALUE outside an explicit transaction block
    # as long as the new value isn't used in the same one — true here.
    op.execute("ALTER TYPE review_kind ADD VALUE IF NOT EXISTS 'pii_review'")


def downgrade() -> None:
    # Postgres has no DROP VALUE — same as every other enum-value migration here.
    pass
