"""add_provider_call_result_ref

Revision ID: e6f7a8b9c0d2
Revises: b9c8d7e6f5a4
Create Date: 2026-10-30

专项「阶段级 Checkpoint 与 AI 调用控制」S3 收口：provider_calls 增加成功结果持久化引用
（result_ref JSONB），用于关闭「网关成功提交至业务 Checkpoint 间」的失联窗口（CA-07），
恢复时复用已成功结果而非重新外发/收费。仅新增可空列，不回填、不删除既有数据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e6f7a8b9c0d2'
down_revision: Union[str, None] = 'b9c8d7e6f5a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'provider_calls',
        sa.Column('result_ref', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('provider_calls', 'result_ref')
