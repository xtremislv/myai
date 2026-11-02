"""Per-user chat keys; chat_history no longer holds message text

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("chat_key_salt", sa.LargeBinary(), nullable=True))
        batch.add_column(sa.Column("chat_key_nonce", sa.LargeBinary(), nullable=True))
        batch.add_column(sa.Column("wrapped_chat_key", sa.LargeBinary(), nullable=True))
    with op.batch_alter_table("chat_history") as batch:
        batch.drop_column("text")


def downgrade() -> None:
    with op.batch_alter_table("chat_history") as batch:
        batch.add_column(sa.Column("text", sa.Text(), nullable=False, server_default=""))
    with op.batch_alter_table("users") as batch:
        batch.drop_column("wrapped_chat_key")
        batch.drop_column("chat_key_nonce")
        batch.drop_column("chat_key_salt")
