"""channel_videos and channel_syncs: uploads of our and competitors' YouTube channels, for the scouts

Revision ID: 0013
Revises: 0012
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'channel_videos',
        sa.Column('id', sa.String(length=16), nullable=False),
        sa.Column('channel_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('views', sa.BigInteger(), nullable=False),
        sa.Column('synced_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_channel_videos_channel_id'), 'channel_videos', ['channel_id'], unique=False)
    op.create_table(
        'channel_syncs',
        sa.Column('channel_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('refs', sa.JSON(), nullable=False),
        sa.Column('videos', sa.Integer(), nullable=False),
        sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('stats_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('channel_id'),
    )


def downgrade() -> None:
    op.drop_table('channel_syncs')
    op.drop_index(op.f('ix_channel_videos_channel_id'), table_name='channel_videos')
    op.drop_table('channel_videos')
