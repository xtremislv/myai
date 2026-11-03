"""Company document templates

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('doc_templates',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('scope', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('description', sa.String(length=512), nullable=False),
    sa.Column('filename', sa.String(length=256), nullable=False),
    sa.Column('content', sa.LargeBinary(), nullable=False),
    sa.Column('created_by', sa.String(length=128), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('scope', 'name', name='uq_doc_templates_scope_name')
    )
    op.create_index(op.f('ix_doc_templates_scope'), 'doc_templates', ['scope'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_doc_templates_scope'), table_name='doc_templates')
    op.drop_table('doc_templates')
    
