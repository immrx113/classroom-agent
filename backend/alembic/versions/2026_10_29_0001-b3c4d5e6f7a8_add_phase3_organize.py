"""add_phase3_organize

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-10-29

第三阶段「全课整理」：分块清单、块执行状态、索引交接意向，以及笔记发布/编辑追溯字段。
仅新增结构，不做数据回填；阶段二数据与稳定 ID/引用保持不动（P03-16）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. note_versions：发布/编辑追溯字段
    op.add_column('note_versions', sa.Column('previous_version_id', sa.UUID(), nullable=True))
    op.add_column('note_versions', sa.Column('edit_source', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('note_versions', sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('note_versions', sa.Column('published_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        'fk_note_versions_previous', 'note_versions', 'note_versions',
        ['previous_version_id'], ['id'], ondelete='SET NULL',
    )

    # 2. meetings：当前有效笔记版本指针
    op.add_column('meetings', sa.Column('active_note_version_id', sa.UUID(), nullable=True))

    # 3. chunk_manifests
    op.create_table(
        'chunk_manifests',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('lecture_id', sa.UUID(), nullable=False),
        sa.Column('transcript_version_id', sa.UUID(), nullable=False),
        sa.Column('input_segment_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('chunk_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['processing_jobs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['lecture_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['transcript_version_id'], ['transcript_versions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_chunk_manifests_job', 'chunk_manifests', ['job_id'], unique=False)

    # 4. chunk_items
    op.create_table(
        'chunk_items',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('manifest_id', sa.UUID(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('core_segments', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('context_segments', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('status', sa.String(length=50), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('attempt', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('output', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error_code', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['manifest_id'], ['chunk_manifests.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('manifest_id', 'chunk_index', name='uq_chunk_item_manifest_index'),
    )
    op.create_index('idx_chunk_items_manifest', 'chunk_items', ['manifest_id'], unique=False)

    # 5. index_intents
    op.create_table(
        'index_intents',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('note_version_id', sa.UUID(), nullable=False),
        sa.Column('source_version_id', sa.UUID(), nullable=True),
        sa.Column('status', sa.String(length=50), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['note_version_id'], ['note_versions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_version_id'], ['transcript_versions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('note_version_id', name='uq_index_intents_note_version'),
    )


def downgrade() -> None:
    op.drop_table('index_intents')
    op.drop_index('idx_chunk_items_manifest', table_name='chunk_items')
    op.drop_table('chunk_items')
    op.drop_index('idx_chunk_manifests_job', table_name='chunk_manifests')
    op.drop_table('chunk_manifests')
    op.drop_column('meetings', 'active_note_version_id')
    op.drop_constraint('fk_note_versions_previous', 'note_versions', type_='foreignkey')
    op.drop_column('note_versions', 'published_at')
    op.drop_column('note_versions', 'edited_at')
    op.drop_column('note_versions', 'edit_source')
    op.drop_column('note_versions', 'previous_version_id')
