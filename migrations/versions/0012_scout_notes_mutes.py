"""scout_findings.review_note (why a person kept/rejected a find) and scout_mutes (per-person mute)

Revision ID: 0012
Revises: 0011
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('scout_findings', sa.Column('review_note', sa.Text(), server_default='', nullable=False))
    op.create_table(
        'scout_mutes',
        sa.Column('scout_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('scout_id', 'user_id'),
    )


def downgrade() -> None:
    op.drop_table('scout_mutes')
    op.drop_column('scout_findings', 'review_note')
