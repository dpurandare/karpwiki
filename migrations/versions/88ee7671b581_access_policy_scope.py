"""access_policy scope column (page_type/tag fine-grained grants)

Revision ID: 88ee7671b581
Revises: 7cc67060951f
Create Date: 2026-08-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '88ee7671b581'
down_revision = '7cc67060951f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fine-grained (per-page_type) access control (07 §2, phase3-tasklist.md step 70).
    # `""` (the server default, backfilling every existing row) means "workspace-wide" —
    # today's only grant shape, unchanged in meaning. A non-empty `scope` (`page_type:X`)
    # narrows a grant to just that page_type; a page_type only becomes restricted once at
    # least one such row exists for it, so an untouched workspace behaves exactly as before.
    op.add_column(
        'access_policy',
        sa.Column('scope', sa.String(length=64), nullable=False, server_default=''),
    )
    op.drop_constraint('access_policy_pkey', 'access_policy', type_='primary')
    op.create_primary_key(
        'access_policy_pkey', 'access_policy', ['workspace_id', 'principal', 'scope']
    )


def downgrade() -> None:
    op.drop_constraint('access_policy_pkey', 'access_policy', type_='primary')
    op.create_primary_key('access_policy_pkey', 'access_policy', ['workspace_id', 'principal'])
    op.drop_column('access_policy', 'scope')
