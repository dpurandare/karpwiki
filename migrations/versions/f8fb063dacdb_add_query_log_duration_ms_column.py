"""add query_log duration_ms column

Revision ID: f8fb063dacdb
Revises: e2bd2860a135
Create Date: 2026-08-19 13:21:58.880445
"""

from alembic import op
import sqlalchemy as sa


revision = 'f8fb063dacdb'
down_revision = 'e2bd2860a135'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('query_log', sa.Column('duration_ms', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('query_log', 'duration_ms')
