"""add raw_source superseded_at column

Revision ID: da3c87c7d151
Revises: 20102d0aa751
Create Date: 2026-08-18 10:21:05.124615
"""

from alembic import op
import sqlalchemy as sa


revision = 'da3c87c7d151'
down_revision = '20102d0aa751'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('raw_source', sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('raw_source', 'superseded_at')
