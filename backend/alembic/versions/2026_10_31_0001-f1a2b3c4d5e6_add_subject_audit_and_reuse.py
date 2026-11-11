"""add_subject_audit_and_reuse

Revision ID: f1a2b3c4d5e6
Revises: e6f7a8b9c0d2
Create Date: 2026-10-31

专项「阶段级 Checkpoint 与 AI 调用控制」S4 收口（CA-24 可观测性与范围隔离）：

1. provider_calls 增加 ``subject_id``（可空，不索引旧数据回填）：IndexIntent 等无 job 的
   实际外发调用也能按归属主体审计与计数，纳入本人请求统计。
2. processing_jobs 增加 ``reuse_count``（默认 0）：恢复时命中已成功 Checkpoint、跳过
   外部调用的复用次数，与 ``resume_count``（网关恢复）区分于「成功产物存量」。

仅新增可空列 / 带默认值列，不回填、不删除既有数据（CA-25 不破坏旧账本/审计）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e6f7a8b9c0d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'provider_calls',
        sa.Column('subject_id', sa.String(length=100), nullable=True),
    )
    op.create_index('ix_provider_calls_subject_id', 'provider_calls', ['subject_id'], unique=False)

    op.add_column(
        'processing_jobs',
        sa.Column('reuse_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
    )


def downgrade() -> None:
    op.drop_column('processing_jobs', 'reuse_count')
    op.drop_index('ix_provider_calls_subject_id', table_name='provider_calls')
    op.drop_column('provider_calls', 'subject_id')
