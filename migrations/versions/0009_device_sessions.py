"""Device sessions for the Android app: refresh tokens, device-wrapped chat key, push token

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table('device_sessions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('owner_id', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('platform', sa.String(length=16), nullable=False),
    sa.Column('refresh_hash', sa.String(length=64), nullable=False),
    sa.Column('prev_refresh_hash', sa.String(length=64), nullable=True),
    sa.Column('key_nonce', sa.LargeBinary(), nullable=True),
    sa.Column('wrapped_chat_key', sa.LargeBinary(), nullable=True),
    sa.Column('push_token', sa.String(length=512), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_device_sessions_owner_id'), 'device_sessions', ['owner_id'], unique=False)
    op.create_index(op.f('ix_device_sessions_refresh_hash'), 'device_sessions', ['refresh_hash'], unique=False)



def downgrade() -> None:

    op.drop_index(op.f('ix_device_sessions_refresh_hash'), table_name='device_sessions')
    op.drop_index(op.f('ix_device_sessions_owner_id'), table_name='device_sessions')
    op.drop_table('device_sessions')
