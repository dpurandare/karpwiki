"""query_feedback table (search result feedback loop)

Revision ID: a78315b565c9
Revises: 88ee7671b581
Create Date: 2026-08-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'a78315b565c9'
down_revision = '88ee7671b581'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `create_type=False` on the column's own ENUM: without it, `create_table` below issues
    # its own `CREATE TYPE` in addition to the explicit one on the next line, which fails
    # with "type already exists" and rolls back the whole migration (found live — the exact
    # same class of Alembic/Postgres-ENUM footgun the connector_state migration hit at
    # step 51, 09's own decision log).
    feedback_rating = postgresql.ENUM('up', 'down', name='feedback_rating', create_type=False)
    feedback_rating.create(op.get_bind())
    op.create_table(
        'query_feedback',
        sa.Column('feedback_id', sa.UUID(), nullable=False),
        sa.Column('query_id', sa.UUID(), nullable=False),
        sa.Column('page_id', sa.UUID(), nullable=False),
        sa.Column('principal', sa.String(length=255), nullable=False),
        sa.Column('rating', feedback_rating, nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['page_id'], ['wiki_page.page_id']),
        sa.ForeignKeyConstraint(['query_id'], ['query_log.query_id']),
        sa.PrimaryKeyConstraint('feedback_id'),
    )
    op.create_index(op.f('ix_query_feedback_query_id'), 'query_feedback', ['query_id'], unique=False)
    op.create_index(op.f('ix_query_feedback_page_id'), 'query_feedback', ['page_id'], unique=False)
    op.create_index(op.f('ix_query_feedback_principal'), 'query_feedback', ['principal'], unique=False)
    op.create_index(op.f('ix_query_feedback_created_at'), 'query_feedback', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_query_feedback_created_at'), table_name='query_feedback')
    op.drop_index(op.f('ix_query_feedback_principal'), table_name='query_feedback')
    op.drop_index(op.f('ix_query_feedback_page_id'), table_name='query_feedback')
    op.drop_index(op.f('ix_query_feedback_query_id'), table_name='query_feedback')
    op.drop_table('query_feedback')
    postgresql.ENUM(name='feedback_rating').drop(op.get_bind())
