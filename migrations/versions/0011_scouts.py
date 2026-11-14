"""scouts, scout_runs, scout_findings: standing research tasks (app/scout.py)

Revision ID: 0011
Revises: 0010
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'scouts',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('department', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('settings', sa.JSON(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_by', sa.String(length=128), nullable=False),
        sa.Column('updated_by', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('running_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cursor', sa.Integer(), server_default='0', nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_scouts_department'), 'scouts', ['department'], unique=False)
    op.create_index(op.f('ix_scouts_next_run_at'), 'scouts', ['next_run_at'], unique=False)
    op.create_table(
        'scout_runs',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('scout_id', sa.String(length=64), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('trigger', sa.String(length=16), nullable=False),
        sa.Column('queries', sa.JSON(), nullable=False),
        sa.Column('candidates', sa.Integer(), nullable=False),
        sa.Column('judged', sa.Integer(), nullable=False),
        sa.Column('added', sa.Integer(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_scout_runs_scout_id'), 'scout_runs', ['scout_id'], unique=False)
    op.create_table(
        'scout_findings',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('scout_id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('key', sa.String(length=160), nullable=False),
        sa.Column('name', sa.String(length=300), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('why', sa.Text(), nullable=False),
        sa.Column('scores', sa.JSON(), nullable=False),
        sa.Column('total', sa.Float(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('video_url', sa.String(length=300), nullable=False),
        sa.Column('video_title', sa.String(length=300), nullable=False),
        sa.Column('video_views', sa.Integer(), nullable=False),
        sa.Column('product_url', sa.String(length=600), nullable=False),
        sa.Column('price', sa.String(length=64), nullable=False),
        sa.Column('source', sa.String(length=600), nullable=False),
        sa.Column('found_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('reviewed_by', sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('scout_id', 'key', name='uq_scout_finding_key'),
    )
    op.create_index(op.f('ix_scout_findings_scout_id'), 'scout_findings', ['scout_id'], unique=False)
    op.create_index(op.f('ix_scout_findings_status'), 'scout_findings', ['status'], unique=False)


def downgrade() -> None:
    op.drop_table('scout_findings')
    op.drop_table('scout_runs')
    op.drop_table('scouts')
