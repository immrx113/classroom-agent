"""add_chunk_item_timing

Revision ID: 7d8e9f0a1b2c
Revises: f1a2b3c4d5e6
Create Date: 2026-11-10

阶段 3 返工 A6：为 chunk_items 增加 ``started_at`` / ``finished_at``（可空），
使「四类分支的耗时/错误/重试/用量」在非网关路径（无 ProviderCall）下也能从实际
运行详情查询。仅新增可空列，不回填、不删除既有数据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '7d8e9f0a1b2c'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('chunk_items', sa.Column('started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('chunk_items', sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('chunk_items', 'finished_at')
    op.drop_column('chunk_items', 'started_at')
