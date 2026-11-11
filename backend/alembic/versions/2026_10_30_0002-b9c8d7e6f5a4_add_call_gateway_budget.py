"""add_call_gateway_budget

Revision ID: b9c8d7e6f5a4
Revises: a9b8c7d6e5f4
Create Date: 2026-10-30

专项「阶段级 Checkpoint 与 AI 调用控制」S3：持久预算账户、预留与供应商并发 permit。
仅新增结构与固定 permit 槽位，不回填、不删除既有数据；旧 SQLite 试用账本迁移在验收时
由 call_budget_service.migrate_legacy_trial 按需执行（CA-19）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b9c8d7e6f5a4'
down_revision: Union[str, None] = 'a9b8c7d6e5f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0. processing_jobs：累计控制等待（CA-17 耗尽判定）
    op.add_column(
        'processing_jobs',
        sa.Column('control_wait_elapsed_seconds', sa.Integer(), nullable=False, server_default=sa.text('0')),
    )

    # 1. budget_accounts：主体日 / 全局日 / 试用累计账户（定点整数微元）
    op.create_table(
        'budget_accounts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('kind', sa.String(length=50), nullable=False),
        sa.Column('account_key', sa.String(length=100), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('cap_micro', sa.BigInteger(), nullable=False),
        sa.Column('reserved_micro', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
        sa.Column('settled_micro', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kind', 'account_key', 'day', name='uq_budget_account_kind_key_day'),
    )

    # 2. budget_reservations：attempt_id 唯一预留/结算
    op.create_table(
        'budget_reservations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('attempt_id', sa.UUID(), nullable=False),
        sa.Column('operation_key', sa.String(length=300), nullable=False),
        sa.Column('attempt_no', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('subject_id', sa.String(length=100), nullable=False),
        sa.Column('reserved_micro', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
        sa.Column('settled_micro', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
        sa.Column('usage_state', sa.String(length=50), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False, server_default=sa.text("'reserved'")),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('attempt_id', name='uq_budget_reservations_attempt'),
    )
    op.create_index('ix_budget_reservations_operation_key', 'budget_reservations', ['operation_key'], unique=False)

    # 3. provider_permit_slots：固定槽位（slot 0/1），供并发 permit 抢占
    op.create_table(
        'provider_permit_slots',
        sa.Column('slot', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=True),
        sa.Column('lease_owner', sa.String(length=100), nullable=True),
        sa.Column('lease_epoch', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('operation_key', sa.String(length=300), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('slot'),
    )
    # 固定 2 个空槽位（CHECKPOINT_PROVIDER_PERMIT_MAX=2）
    op.execute(
        "INSERT INTO provider_permit_slots (slot, lease_epoch) VALUES (0, 0), (1, 0)"
    )


def downgrade() -> None:
    op.drop_table('provider_permit_slots')
    op.drop_index('ix_budget_reservations_operation_key', table_name='budget_reservations')
    op.drop_table('budget_reservations')
    op.drop_table('budget_accounts')
    op.drop_column('processing_jobs', 'control_wait_elapsed_seconds')
