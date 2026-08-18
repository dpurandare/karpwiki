"""add review_item detail column

Revision ID: 20102d0aa751
Revises: e139b033f7f3
Create Date: 2026-08-18 10:03:52.303420
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '20102d0aa751'
down_revision = 'e139b033f7f3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('review_item', sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('review_item', 'detail')
