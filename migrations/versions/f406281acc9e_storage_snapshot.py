"""storage_snapshot table (storage/usage trend data)

Revision ID: f406281acc9e
Revises: f27a7aa73ce2
Create Date: 2026-08-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'f406281acc9e'
down_revision = 'f27a7aa73ce2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'storage_snapshot',
        sa.Column('snapshot_id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.String(length=64), nullable=False),
        sa.Column('object_store_bytes', sa.BigInteger(), nullable=False),
        sa.Column('metadata_db_bytes_approx', sa.BigInteger(), nullable=False),
        sa.Column('fts_index_bytes_approx', sa.BigInteger(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspace.workspace_id']),
        sa.PrimaryKeyConstraint('snapshot_id'),
    )
    op.create_index(op.f('ix_storage_snapshot_workspace_id'), 'storage_snapshot', ['workspace_id'], unique=False)
    op.create_index(op.f('ix_storage_snapshot_created_at'), 'storage_snapshot', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_storage_snapshot_created_at'), table_name='storage_snapshot')
    op.drop_index(op.f('ix_storage_snapshot_workspace_id'), table_name='storage_snapshot')
    op.drop_table('storage_snapshot')
