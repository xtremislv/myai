"""users.session_version: bumped to sign every session of a person out (password change, reset, sign-out)

Revision ID: 0010
Revises: 0009
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('session_version', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'session_version')
