"""add_phase4_retrieval

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-10-29

第四阶段「检索闭环」：文档身份/版本扩展、解析块（真实定位）、索引版本与索引块。
仅新增结构，不做数据回填；阶段三及更早数据与稳定 ID/引用保持不动（P04-16）。
旧未归属课堂知识记录（knowledge_documents）保持原样，不默认全库召回。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 1. source_documents：文件身份字段
    op.add_column('source_documents', sa.Column('filename', sa.String(length=500), nullable=True))
    op.add_column('source_documents', sa.Column('content_hash', sa.String(length=128), nullable=True))

    # 2. source_document_versions：解析产物状态与配置
    op.add_column('source_document_versions', sa.Column('parse_status', sa.String(length=50), server_default=sa.text("'parsed'"), nullable=False))
    op.add_column('source_document_versions', sa.Column('error_code', sa.String(length=100), nullable=True))
    op.add_column('source_document_versions', sa.Column('content_hash', sa.String(length=128), nullable=True))
    op.add_column('source_document_versions', sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('source_document_versions', sa.Column('block_count', sa.Integer(), server_default=sa.text('0'), nullable=False))

    # 3. parsed_blocks
    op.create_table(
        'parsed_blocks',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('document_version_id', sa.UUID(), nullable=False),
        sa.Column('block_id', sa.String(length=200), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=50), server_default=sa.text("'text'"), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('heading', sa.String(length=500), nullable=True),
        sa.Column('page_no', sa.Integer(), nullable=True),
        sa.Column('paragraph_index', sa.Integer(), nullable=True),
        sa.Column('table_index', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['document_version_id'], ['source_document_versions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_version_id', 'block_id', name='uq_parsed_block_version_block'),
    )
    op.create_index('idx_parsed_blocks_version', 'parsed_blocks', ['document_version_id'], unique=False)

    # 4. index_versions
    op.create_table(
        'index_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('course_id', sa.UUID(), nullable=False),
        sa.Column('status', sa.String(length=50), server_default=sa.text("'building'"), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('source_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('block_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('error_code', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_index_versions_course', 'index_versions', ['course_id'], unique=False)
    op.create_index(
        'uq_index_versions_course_active',
        'index_versions',
        ['course_id'],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # 5. index_blocks
    op.create_table(
        'index_blocks',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('index_version_id', sa.UUID(), nullable=False),
        sa.Column('course_id', sa.UUID(), nullable=False),
        sa.Column('lecture_id', sa.UUID(), nullable=True),
        sa.Column('source_type', sa.String(length=50), nullable=False),
        sa.Column('source_transcript_version_id', sa.UUID(), nullable=True),
        sa.Column('source_document_version_id', sa.UUID(), nullable=True),
        sa.Column('source_note_version_id', sa.UUID(), nullable=True),
        sa.Column('block_id', sa.String(length=200), nullable=True),
        sa.Column('page_no', sa.Integer(), nullable=True),
        sa.Column('segment_start', sa.Integer(), nullable=True),
        sa.Column('segment_end', sa.Integer(), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', Vector(1024), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['index_version_id'], ['index_versions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "source_type IN ('transcript', 'document', 'note')",
            name='ck_index_blocks_source_type',
        ),
        sa.CheckConstraint(
            "source_type != 'transcript' OR "
            "(source_transcript_version_id IS NOT NULL AND source_document_version_id IS NULL "
            " AND source_note_version_id IS NULL)",
            name='ck_index_blocks_transcript_source',
        ),
        sa.CheckConstraint(
            "source_type != 'document' OR "
            "(source_document_version_id IS NOT NULL AND source_transcript_version_id IS NULL "
            " AND source_note_version_id IS NULL)",
            name='ck_index_blocks_document_source',
        ),
        sa.CheckConstraint(
            "source_type != 'note' OR "
            "(source_note_version_id IS NOT NULL AND source_transcript_version_id IS NULL "
            " AND source_document_version_id IS NULL)",
            name='ck_index_blocks_note_source',
        ),
    )
    op.create_index('idx_index_blocks_version', 'index_blocks', ['index_version_id'], unique=False)
    op.create_index('idx_index_blocks_course', 'index_blocks', ['course_id'], unique=False)
    op.create_index('idx_index_blocks_lecture', 'index_blocks', ['lecture_id'], unique=False)
    op.create_index('uq_index_blocks_version_chunk', 'index_blocks', ['index_version_id', 'chunk_index'], unique=True)


def downgrade() -> None:
    op.drop_index('uq_index_blocks_version_chunk', table_name='index_blocks')
    op.drop_index('idx_index_blocks_lecture', table_name='index_blocks')
    op.drop_index('idx_index_blocks_course', table_name='index_blocks')
    op.drop_index('idx_index_blocks_version', table_name='index_blocks')
    op.drop_table('index_blocks')
    op.drop_index('uq_index_versions_course_active', table_name='index_versions')
    op.drop_index('idx_index_versions_course', table_name='index_versions')
    op.drop_table('index_versions')
    op.drop_index('idx_parsed_blocks_version', table_name='parsed_blocks')
    op.drop_table('parsed_blocks')
    op.drop_column('source_document_versions', 'block_count')
    op.drop_column('source_document_versions', 'config')
    op.drop_column('source_document_versions', 'content_hash')
    op.drop_column('source_document_versions', 'error_code')
    op.drop_column('source_document_versions', 'parse_status')
    op.drop_column('source_documents', 'content_hash')
    op.drop_column('source_documents', 'filename')
