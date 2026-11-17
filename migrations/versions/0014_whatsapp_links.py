"""whatsapp_links: a person's linked WhatsApp number, link code, conversation version, result page, document text

Revision ID: 0014
Revises: 0013
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'whatsapp_links',
        sa.Column('user_id', sa.String(length=128), nullable=False),
        sa.Column('phone', sa.String(length=20), nullable=True),
        sa.Column('pending_phone', sa.String(length=20), nullable=True),
        sa.Column('code_hash', sa.String(length=64), nullable=True),
        sa.Column('code_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('linked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_inbound_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('thread_version', sa.Integer(), nullable=False),
        sa.Column('notify', sa.Boolean(), nullable=False),
        sa.Column('page_version', sa.Integer(), nullable=False),
        sa.Column('page_token_hash', sa.String(length=64), nullable=True),
        sa.Column('page', sa.Text(), nullable=True),
        sa.Column('page_updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('files', sa.Text(), nullable=True),
        sa.Column('files_expire_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('user_id'),
        sa.UniqueConstraint('phone'),
        sa.UniqueConstraint('page_token_hash'),
    )
    op.create_index(op.f('ix_whatsapp_links_pending_phone'), 'whatsapp_links', ['pending_phone'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_whatsapp_links_pending_phone'), table_name='whatsapp_links')
    op.drop_table('whatsapp_links')
