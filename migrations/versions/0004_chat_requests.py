"""Link approvals and inbox tasks to the chat they came from; keep approval results

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("chat_thread", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("result", sa.Text(), nullable=True))
        batch.create_index(batch.f("ix_approvals_chat_thread"), ["chat_thread"])
    with op.batch_alter_table("department_tasks") as batch:
        batch.add_column(sa.Column("chat_thread", sa.String(length=128), nullable=True))
        batch.create_index(batch.f("ix_department_tasks_chat_thread"), ["chat_thread"])


def downgrade() -> None:
    with op.batch_alter_table("department_tasks") as batch:
        batch.drop_index(batch.f("ix_department_tasks_chat_thread"))
        batch.drop_column("chat_thread")
    with op.batch_alter_table("approvals") as batch:
        batch.drop_index(batch.f("ix_approvals_chat_thread"))
        batch.drop_column("result")
        batch.drop_column("chat_thread")
