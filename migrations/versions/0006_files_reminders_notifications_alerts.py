"""Personal files, reminders, notifications and ops alerts; user preferences

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    
    op.create_table('notifications',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('owner_id', sa.String(length=128), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('encrypted_title', sa.Text(), nullable=False),
    sa.Column('encrypted_body', sa.Text(), nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notifications_created_at'), 'notifications', ['created_at'], unique=False)
    op.create_index(op.f('ix_notifications_owner_id'), 'notifications', ['owner_id'], unique=False)
    op.create_table('ops_alerts',
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('severity', sa.String(length=32), nullable=False),
    sa.Column('summary', sa.String(length=512), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('fingerprint')
    )
    op.create_table('reminders',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('owner_id', sa.String(length=128), nullable=False),
    sa.Column('encrypted_text', sa.Text(), nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('repeat', sa.String(length=16), nullable=False),
    sa.Column('email', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('source_ref', sa.String(length=64), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_reminders_due_at'), 'reminders', ['due_at'], unique=False)
    op.create_index(op.f('ix_reminders_owner_id'), 'reminders', ['owner_id'], unique=False)
    op.create_index(op.f('ix_reminders_source_ref'), 'reminders', ['source_ref'], unique=False)
    op.create_index(op.f('ix_reminders_state'), 'reminders', ['state'], unique=False)
    op.create_table('user_files',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('owner_id', sa.String(length=128), nullable=False),
    sa.Column('encrypted_path', sa.Text(), nullable=False),
    sa.Column('content_type', sa.String(length=128), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('versions', sa.JSON(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_user_files_owner_id'), 'user_files', ['owner_id'], unique=False)
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('prefs', sa.JSON(), server_default='{}', nullable=False))
        batch.add_column(sa.Column('last_digest_at', sa.DateTime(timezone=True), nullable=True))
    


def downgrade() -> None:
    
    with op.batch_alter_table('users') as batch:
        batch.drop_column('last_digest_at')
        batch.drop_column('prefs')
    op.drop_index(op.f('ix_user_files_owner_id'), table_name='user_files')
    op.drop_table('user_files')
    op.drop_index(op.f('ix_reminders_state'), table_name='reminders')
    op.drop_index(op.f('ix_reminders_source_ref'), table_name='reminders')
    op.drop_index(op.f('ix_reminders_owner_id'), table_name='reminders')
    op.drop_index(op.f('ix_reminders_due_at'), table_name='reminders')
    op.drop_table('reminders')
    op.drop_table('ops_alerts')
    op.drop_index(op.f('ix_notifications_owner_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_created_at'), table_name='notifications')
    op.drop_table('notifications')
    
