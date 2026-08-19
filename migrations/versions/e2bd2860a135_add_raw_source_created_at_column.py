"""add raw_source created_at column

Revision ID: e2bd2860a135
Revises: da3c87c7d151
Create Date: 2026-08-19 12:38:42.051097
"""

from alembic import op
import sqlalchemy as sa


revision = 'e2bd2860a135'
down_revision = 'da3c87c7d151'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'raw_source',
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('clock_timestamp()'),
        ),
    )


def downgrade() -> None:
    op.drop_column('raw_source', 'created_at')
