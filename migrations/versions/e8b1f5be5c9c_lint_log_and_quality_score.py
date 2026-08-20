"""lint_log table and wiki_page.quality_score (content quality scoring)

Revision ID: e8b1f5be5c9c
Revises: a78315b565c9
Create Date: 2026-08-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'e8b1f5be5c9c'
down_revision = 'a78315b565c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wiki_page', sa.Column('quality_score', sa.Float(), nullable=True))
    op.create_table(
        'lint_log',
        sa.Column('entry_id', sa.UUID(), nullable=False),
        sa.Column('page_id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=64), nullable=False),
        sa.Column('detail', postgresql.JSONB(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['page_id'], ['wiki_page.page_id']),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspace.workspace_id']),
        sa.PrimaryKeyConstraint('entry_id'),
    )
    op.create_index(op.f('ix_lint_log_page_id'), 'lint_log', ['page_id'], unique=False)
    op.create_index(op.f('ix_lint_log_workspace_id'), 'lint_log', ['workspace_id'], unique=False)
    op.create_index(op.f('ix_lint_log_created_at'), 'lint_log', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_lint_log_created_at'), table_name='lint_log')
    op.drop_index(op.f('ix_lint_log_workspace_id'), table_name='lint_log')
    op.drop_index(op.f('ix_lint_log_page_id'), table_name='lint_log')
    op.drop_table('lint_log')
    op.drop_column('wiki_page', 'quality_score')
