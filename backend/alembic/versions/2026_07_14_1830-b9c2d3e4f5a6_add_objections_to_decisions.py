"""add_objections_to_decisions

Revision ID: b9c2d3e4f5a6
Revises: a8e1fa73aad0
Create Date: 2026-07-14 18:30:00.000000+00:00

评审决策表补全：添加 objections 字段（反对意见，少数派观点）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b9c2d3e4f5a6'
down_revision: Union[str, None] = 'a8e1fa73aad0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 添加 objections 字段到 decisions 表
    op.add_column(
        'decisions',
        sa.Column(
            'objections',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('decisions', 'objections')