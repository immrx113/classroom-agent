"""add_checkpoint_ai_control

Revision ID: a9b8c7d6e5f4
Revises: c4d5e6f7a8b9
Create Date: 2026-10-30

专项「阶段级 Checkpoint 与 AI 调用控制」S1：任务身份、Checkpoint、调用记录与租约。
仅新增结构与可空列，不回填、不删除既有四阶段数据与引用（CA-25 的旧库迁移在 S4 完整验收）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a9b8c7d6e5f4'
down_revision: Union[str, None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. processing_jobs：任务身份 / 配置快照 / 策略版本 / 租约 / 可观测字段
    op.add_column('processing_jobs', sa.Column('subject_id', sa.String(length=100), nullable=True))
    op.add_column('processing_jobs', sa.Column('idempotency_key', sa.String(length=300), nullable=True))
    op.add_column('processing_jobs', sa.Column('input_snapshot_ref', sa.String(length=200), nullable=True))
    op.add_column('processing_jobs', sa.Column('config_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('processing_jobs', sa.Column('policy_version', sa.String(length=50), nullable=True))
    op.add_column('processing_jobs', sa.Column('lease_owner', sa.String(length=100), nullable=True))
    op.add_column('processing_jobs', sa.Column('lease_epoch', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('processing_jobs', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('processing_jobs', sa.Column('wait_reason', sa.String(length=50), nullable=True))
    op.add_column('processing_jobs', sa.Column('last_successful_stage', sa.String(length=50), nullable=True))
    op.add_column('processing_jobs', sa.Column('resume_count', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.create_index('uq_processing_jobs_idempotency_key', 'processing_jobs', ['idempotency_key'], unique=True)
    op.create_index('ix_processing_jobs_subject_id', 'processing_jobs', ['subject_id'], unique=False)

    # 2. job_checkpoints
    op.create_table(
        'job_checkpoints',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('step_key', sa.String(length=100), nullable=False),
        sa.Column('unit_key', sa.String(length=200), nullable=False),
        sa.Column('input_hash', sa.String(length=128), nullable=True),
        sa.Column('config_hash', sa.String(length=128), nullable=True),
        sa.Column('status', sa.String(length=50), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('output_ref', sa.String(length=500), nullable=True),
        sa.Column('output_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('output_hash', sa.String(length=128), nullable=True),
        sa.Column('schema_version', sa.String(length=50), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['processing_jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id', 'step_key', 'unit_key', name='uq_job_checkpoint_job_step_unit'),
    )
    op.create_index('ix_job_checkpoints_job_id', 'job_checkpoints', ['job_id'], unique=False)

    # 3. provider_calls
    op.create_table(
        'provider_calls',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('operation_key', sa.String(length=300), nullable=False),
        sa.Column('attempt_no', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=True),
        sa.Column('checkpoint_id', sa.UUID(), nullable=True),
        sa.Column('provider', sa.String(length=100), nullable=True),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('operation_type', sa.String(length=50), nullable=True),
        sa.Column('input_hash', sa.String(length=128), nullable=True),
        sa.Column('provider_task_id', sa.String(length=200), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('error_code', sa.String(length=100), nullable=True),
        sa.Column('retry_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('usage', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('reserved_amount', sa.Integer(), nullable=True),
        sa.Column('settled_amount', sa.Integer(), nullable=True),
        sa.Column('usage_state', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['processing_jobs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('operation_key', 'attempt_no', name='uq_provider_calls_op_attempt'),
    )
    op.create_index('ix_provider_calls_job_id', 'provider_calls', ['job_id'], unique=False)
    op.create_index('ix_provider_calls_operation_key', 'provider_calls', ['operation_key'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_provider_calls_operation_key', table_name='provider_calls')
    op.drop_index('ix_provider_calls_job_id', table_name='provider_calls')
    op.drop_table('provider_calls')
    op.drop_index('ix_job_checkpoints_job_id', table_name='job_checkpoints')
    op.drop_table('job_checkpoints')
    op.drop_index('ix_processing_jobs_subject_id', table_name='processing_jobs')
    op.drop_index('uq_processing_jobs_idempotency_key', table_name='processing_jobs')
    op.drop_column('processing_jobs', 'resume_count')
    op.drop_column('processing_jobs', 'last_successful_stage')
    op.drop_column('processing_jobs', 'wait_reason')
    op.drop_column('processing_jobs', 'lease_expires_at')
    op.drop_column('processing_jobs', 'lease_epoch')
    op.drop_column('processing_jobs', 'lease_owner')
    op.drop_column('processing_jobs', 'policy_version')
    op.drop_column('processing_jobs', 'config_snapshot')
    op.drop_column('processing_jobs', 'input_snapshot_ref')
    op.drop_column('processing_jobs', 'idempotency_key')
    op.drop_column('processing_jobs', 'subject_id')
