"""add_classroom_domain_tables

Revision ID: e5f6a7b8c9d0
Revises: 293138585702
Create Date: 2026-10-27

阶段一数据基础：课程/课时/版本/证据领域表 + meetings 课程归属列（仅结构）。

- 兼容期：课时沿用 meetings 物理表，新增 course_id / legacy_meeting / lecture_date
  与有效版本指针列；对外接口使用 lecture 语义。
- 数据回填不在本 revision 执行，统一由补偿 revision（下一个 revision）通过
  app.db.backfill 完成，避免旧数据重复序号在结构迁移阶段抛出 IntegrityError。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = '293138585702'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 课程
    op.create_table(
        'courses',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.String(length=100), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('teacher', sa.String(length=100), nullable=True),
        sa.Column('term', sa.String(length=100), nullable=True),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_courses_owner', 'courses', ['owner_id'], unique=False)

    # 2. meetings 增加课程归属与版本指针（transcription_mode 补齐 ORM/迁移差异）
    op.add_column('meetings', sa.Column('course_id', sa.UUID(), nullable=True))
    op.add_column('meetings', sa.Column('legacy_meeting', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('meetings', sa.Column('lecture_date', sa.Date(), nullable=True))
    op.add_column('meetings', sa.Column('active_audio_id', sa.UUID(), nullable=True))
    op.add_column('meetings', sa.Column('active_transcript_version_id', sa.UUID(), nullable=True))
    op.add_column('meetings', sa.Column('transcription_mode', sa.String(length=20), nullable=True))
    op.create_foreign_key('meetings_course_id_fkey', 'meetings', 'courses', ['course_id'], ['id'], ondelete='SET NULL')
    op.create_index('idx_meetings_course', 'meetings', ['course_id'], unique=False)

    # 3. 音频资产
    op.create_table(
        'audio_assets',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('lecture_id', sa.UUID(), nullable=False),
        sa.Column('storage_key', sa.String(length=500), nullable=False),
        sa.Column('checksum', sa.String(length=128), nullable=True),
        sa.Column('mime', sa.String(length=100), nullable=True),
        sa.Column('duration_ms', sa.BigInteger(), nullable=True),
        sa.Column('size', sa.BigInteger(), nullable=True),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('is_demo', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['lecture_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_audio_assets_lecture', 'audio_assets', ['lecture_id'], unique=False)

    # 4. 转写版本与片段
    op.create_table(
        'transcript_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('audio_asset_id', sa.UUID(), nullable=False),
        sa.Column('lecture_id', sa.UUID(), nullable=False),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=True),
        sa.Column('provider_task_id', sa.String(length=200), nullable=True),
        sa.Column('status', sa.String(length=50), server_default=sa.text("'building'"), nullable=False),
        sa.Column('parent_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['audio_asset_id'], ['audio_assets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['lecture_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_id'], ['transcript_versions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_transcript_versions_audio', 'transcript_versions', ['audio_asset_id'], unique=False)
    op.create_index('idx_transcript_versions_lecture', 'transcript_versions', ['lecture_id'], unique=False)

    op.create_table(
        'transcript_segments',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('version_id', sa.UUID(), nullable=False),
        sa.Column('seq_index', sa.Integer(), nullable=False),
        sa.Column('speaker', sa.String(length=100), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('start_ms', sa.BigInteger(), nullable=True),
        sa.Column('end_ms', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['version_id'], ['transcript_versions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('version_id', 'seq_index', name='uq_transcript_segment_version_seq'),
    )
    op.create_index('idx_transcript_segments_version', 'transcript_segments', ['version_id'], unique=False)

    # 5. 课堂笔记版本
    op.create_table(
        'note_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('lecture_id', sa.UUID(), nullable=False),
        sa.Column('transcript_version_id', sa.UUID(), nullable=True),
        sa.Column('pipeline_version', sa.String(length=100), nullable=True),
        sa.Column('status', sa.String(length=50), server_default=sa.text("'draft'"), nullable=False),
        sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['lecture_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['transcript_version_id'], ['transcript_versions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_note_versions_lecture', 'note_versions', ['lecture_id'], unique=False)

    # 6. 课件源文档与版本
    op.create_table(
        'source_documents',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('course_id', sa.UUID(), nullable=False),
        sa.Column('lecture_id', sa.UUID(), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('storage_key', sa.String(length=500), nullable=True),
        sa.Column('mime', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['lecture_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_source_documents_course', 'source_documents', ['course_id'], unique=False)

    op.create_table(
        'source_document_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('source_document_id', sa.UUID(), nullable=False),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('status', sa.String(length=50), server_default=sa.text("'building'"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['source_document_id'], ['source_documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_source_document_versions_doc', 'source_document_versions', ['source_document_id'], unique=False)

    # 7. 证据与产物关联
    op.create_table(
        'evidence_spans',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('transcript_version_id', sa.UUID(), nullable=True),
        sa.Column('segment_start', sa.Integer(), nullable=True),
        sa.Column('segment_end', sa.Integer(), nullable=True),
        sa.Column('start_ms', sa.BigInteger(), nullable=True),
        sa.Column('end_ms', sa.BigInteger(), nullable=True),
        sa.Column('document_version_id', sa.UUID(), nullable=True),
        sa.Column('block_id', sa.String(length=200), nullable=True),
        sa.Column('page_no', sa.Integer(), nullable=True),
        sa.Column('quote', sa.Text(), nullable=True),
        sa.Column('is_valid', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            '(transcript_version_id IS NOT NULL) != (document_version_id IS NOT NULL)',
            name='ck_evidence_span_single_source',
        ),
        sa.ForeignKeyConstraint(['transcript_version_id'], ['transcript_versions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['document_version_id'], ['source_document_versions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'artifact_evidence',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('artifact_type', sa.String(length=50), nullable=False),
        sa.Column('artifact_id', sa.UUID(), nullable=False),
        sa.Column('evidence_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['evidence_id'], ['evidence_spans.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('artifact_type', 'artifact_id', 'evidence_id', name='uq_artifact_evidence'),
    )

    # 数据回填由补偿 revision 统一执行（app.db.backfill）


def downgrade() -> None:
    op.drop_table('artifact_evidence')
    op.drop_table('evidence_spans')
    op.drop_table('source_document_versions')
    op.drop_table('source_documents')
    op.drop_table('note_versions')
    op.drop_table('transcript_segments')
    op.drop_table('transcript_versions')
    op.drop_table('audio_assets')
    op.drop_index('idx_meetings_course', table_name='meetings')
    op.drop_constraint('meetings_course_id_fkey', 'meetings', type_='foreignkey')
    op.drop_column('meetings', 'transcription_mode')
    op.drop_column('meetings', 'active_transcript_version_id')
    op.drop_column('meetings', 'active_audio_id')
    op.drop_column('meetings', 'lecture_date')
    op.drop_column('meetings', 'legacy_meeting')
    op.drop_column('meetings', 'course_id')
    op.drop_index('idx_courses_owner', table_name='courses')
    op.drop_table('courses')
