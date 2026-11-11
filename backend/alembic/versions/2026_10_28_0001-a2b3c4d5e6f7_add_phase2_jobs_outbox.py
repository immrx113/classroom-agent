"""add_phase2_jobs_outbox

Revision ID: a2b3c4d5e6f7
Revises: f7a8b9c0d1e2
Create Date: 2026-10-28

第二阶段「可靠输入」：新增持久任务 processing_jobs 与事务 outbox outbox_events。
仅新增结构，不做数据回填；阶段一数据与稳定 ID/引用保持不动（P02-15）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'processing_jobs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('type', sa.String(length=50), nullable=False),
        sa.Column('resource_id', sa.UUID(), nullable=False),
        sa.Column('input_version', sa.UUID(), nullable=False),
        sa.Column('config_hash', sa.String(length=128), nullable=True),
        sa.Column('status', sa.String(length=50), server_default=sa.text("'queued'"), nullable=False),
        sa.Column('stage', sa.String(length=50), server_default=sa.text("'created'"), nullable=False),
        sa.Column('attempt', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancel_requested', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('error_code', sa.String(length=100), nullable=True),
        sa.Column('error_type', sa.String(length=50), nullable=True),
        sa.Column('provider_task_id', sa.String(length=200), nullable=True),
        sa.Column('output_version', sa.UUID(), nullable=True),
        sa.Column('retry_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_processing_jobs_resource', 'processing_jobs', ['resource_id'], unique=False)
    op.create_index('idx_processing_jobs_input', 'processing_jobs', ['input_version'], unique=False)

    op.create_table(
        'outbox_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('business_key', sa.String(length=200), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=50), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('attempt', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['processing_jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('business_key', name='uq_outbox_events_business_key'),
    )
    op.create_index('idx_outbox_events_status', 'outbox_events', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_outbox_events_status', table_name='outbox_events')
    op.drop_table('outbox_events')
    op.drop_index('idx_processing_jobs_input', table_name='processing_jobs')
    op.drop_index('idx_processing_jobs_resource', table_name='processing_jobs')
    op.drop_table('processing_jobs')
