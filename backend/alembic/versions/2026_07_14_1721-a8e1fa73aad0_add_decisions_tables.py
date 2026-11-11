"""add_decisions_tables

Revision ID: a8e1fa73aad0
Revises: c3d4e5f6a7b8
Create Date: 2026-07-14 17:21:35.381693+00:00

评审决策知识库三表（Q5 决策）：
- decisions: 决策主表（含 pgvector 向量列）
- decision_options: 决策候选方案
- decision_relations: 决策间关系（写入时即时向量关联，Q9 决策）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import pgvector.sqlalchemy.vector

# revision identifiers, used by Alembic.
revision: str = 'a8e1fa73aad0'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # decisions 主表
    op.create_table(
        'decisions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('meeting_id', sa.UUID(), nullable=True),
        sa.Column('title', sa.String(length=50), nullable=False),
        sa.Column('context', sa.Text(), nullable=True),
        sa.Column('snippet', sa.Text(), nullable=True),
        sa.Column('chosen_option', sa.String(length=30), nullable=True),
        sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('decided_by', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column(
            'embedding',
            pgvector.sqlalchemy.vector.VECTOR(dim=1024),
            nullable=True,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # decision_options 方案表
    op.create_table(
        'decision_options',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('decision_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=30), nullable=False),
        sa.Column('pros', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('cons', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('proposed_by', sa.String(length=50), nullable=True),
        sa.Column('is_chosen', sa.Boolean(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['decision_id'], ['decisions.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
    )

    # decision_relations 关系表
    op.create_table(
        'decision_relations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('source_decision_id', sa.UUID(), nullable=False),
        sa.Column('target_decision_id', sa.UUID(), nullable=False),
        sa.Column('relation_type', sa.String(length=20), nullable=False),
        sa.Column('context', sa.Text(), nullable=True),
        sa.Column('similarity_score', sa.Float(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['source_decision_id'], ['decisions.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['target_decision_id'], ['decisions.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'source_decision_id',
            'target_decision_id',
            name='uq_decision_relation',
        ),
    )

    # ── 索引（autogenerate 不会生成向量索引，手动补充） ──
    # pgvector ivfflat 索引（cosine 距离）
    op.execute(
        "CREATE INDEX idx_decisions_embedding ON decisions "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    op.create_index(
        'idx_decisions_meeting', 'decisions', ['meeting_id'], unique=False
    )
    op.create_index(
        'idx_decision_options_decision',
        'decision_options',
        ['decision_id'],
        unique=False,
    )
    op.create_index(
        'idx_decision_relations_source',
        'decision_relations',
        ['source_decision_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('idx_decision_relations_source', table_name='decision_relations')
    op.drop_index('idx_decision_options_decision', table_name='decision_options')
    op.drop_index('idx_decisions_meeting', table_name='decisions')
    op.execute("DROP INDEX IF EXISTS idx_decisions_embedding")
    op.drop_table('decision_relations')
    op.drop_table('decision_options')
    op.drop_table('decisions')
