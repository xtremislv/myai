"""Recovery codes for chat keys; a per-user list of private chat threads

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("recovery_salt", sa.LargeBinary(), nullable=True))
        batch.add_column(sa.Column("recovery_nonce", sa.LargeBinary(), nullable=True))
        batch.add_column(sa.Column("wrapped_recovery_key", sa.LargeBinary(), nullable=True))
    op.create_table(
        "chat_threads",
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("encrypted_title", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "thread_id"),
    )


def downgrade() -> None:
    op.drop_table("chat_threads")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("wrapped_recovery_key")
        batch.drop_column("recovery_nonce")
        batch.drop_column("recovery_salt")
