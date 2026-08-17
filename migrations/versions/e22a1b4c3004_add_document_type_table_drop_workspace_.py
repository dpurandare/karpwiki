"""add document_type table, drop workspace document_types array

Revision ID: e22a1b4c3004
Revises: b199f8900de5
Create Date: 2026-08-17 13:15:38.030038
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e22a1b4c3004'
down_revision = 'b199f8900de5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('document_type',
    sa.Column('type_code', sa.String(length=128), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspace.workspace_id'], ),
    sa.PrimaryKeyConstraint('type_code')
    )
    op.create_index(op.f('ix_document_type_workspace_id'), 'document_type', ['workspace_id'], unique=False)

    # Backfill: one document_type row per entry in each workspace's array column, before the
    # column that held them is dropped below. A type_code appearing in more than one
    # workspace's array (shouldn't happen — 03 §3 assumes a code routes to exactly one
    # workspace — but Phase 1 never enforced that) keeps its first occurrence and drops the
    # rest, since type_code is the new table's primary key.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text('SELECT workspace_id, document_types FROM workspace')
    ).fetchall()
    seen = set()
    for workspace_id, type_codes in rows:
        for type_code in type_codes or []:
            if type_code in seen:
                continue
            seen.add(type_code)
            connection.execute(
                sa.text(
                    'INSERT INTO document_type (type_code, workspace_id) '
                    'VALUES (:type_code, :workspace_id)'
                ),
                {'type_code': type_code, 'workspace_id': workspace_id},
            )

    op.drop_column('workspace', 'document_types')


def downgrade() -> None:
    op.add_column('workspace', sa.Column('document_types', postgresql.ARRAY(sa.VARCHAR()), autoincrement=False, nullable=False, server_default='{}'))

    # Reverse backfill: descriptions have no home in the array column, so they're lost on
    # downgrade — an inherent asymmetry of this schema change, not a bug in the migration.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text('SELECT type_code, workspace_id FROM document_type')
    ).fetchall()
    for type_code, workspace_id in rows:
        connection.execute(
            sa.text(
                'UPDATE workspace SET document_types = array_append(document_types, :type_code) '
                'WHERE workspace_id = :workspace_id'
            ),
            {'type_code': type_code, 'workspace_id': workspace_id},
        )
    op.alter_column('workspace', 'document_types', server_default=None)

    op.drop_index(op.f('ix_document_type_workspace_id'), table_name='document_type')
    op.drop_table('document_type')
