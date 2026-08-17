"""add workspace dedicated_index flag

Revision ID: e139b033f7f3
Revises: e24d56055855
Create Date: 2026-08-17 16:26:19.937803
"""

from alembic import op
import sqlalchemy as sa


revision = 'e139b033f7f3'
down_revision = 'e24d56055855'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so existing rows (any workspace created before this column existed)
    # get `false` rather than failing the NOT NULL constraint; dropped after backfill so
    # the model's own `default=False` (Python-side, insert-time only) stays the only
    # source of truth for new rows.
    op.add_column(
        'workspace',
        sa.Column('dedicated_index', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column('workspace', 'dedicated_index', server_default=None)


def downgrade() -> None:
    op.drop_column('workspace', 'dedicated_index')
