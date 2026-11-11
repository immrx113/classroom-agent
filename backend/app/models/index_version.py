"""检索索引版本与索引块模型

index_versions：一门课程的持久索引版本（building → ready → active → superseded / failed）。
每门课程至多一个 active 版本（部分唯一索引），切换为原子事务：旧 active → superseded，新 → active。

index_blocks：索引块，保留稳定来源身份（source_type + 具体来源版本 + 定位），
用于范围内两路召回、同源去重与来源解析。向量与内容/模型/维度绑定（不混写不可比较向量）。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    String,
    Text,
    Integer,
    DateTime,
    ForeignKey,
    func,
    Index,
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pgvector.sqlalchemy import Vector

from app.db.base import Base


class IndexVersion(Base):
    __tablename__ = "index_versions"
    __table_args__ = (
        # 每门课程至多一个 active 索引版本（原子切换约束）
        Index(
            "uq_index_versions_course_active",
            "course_id",
            unique=True,
            postgresql_where="status = 'active'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # building / ready / active / superseded / failed
    status: Mapped[str] = mapped_column(String(50), default="building")
    # 绑定配置：embedding 模型/维度、内容摘要算法、分块、候选池、top_k、融合/重排、预算等
    config: Mapped[dict | None] = mapped_column(JSONB)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    block_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


_SOURCE_TYPES = ("transcript", "document", "note")


class IndexBlock(Base):
    __tablename__ = "index_blocks"
    __table_args__ = (
        # 同索引版本内块序号唯一（幂等重建不重复写块）
        Index("uq_index_blocks_version_chunk", "index_version_id", "chunk_index", unique=True),
        CheckConstraint(
            "source_type IN ('transcript', 'document', 'note')",
            name="ck_index_blocks_source_type",
        ),
        # 来源唯一：三类来源版本字段互斥且与 source_type 一致
        CheckConstraint(
            "source_type != 'transcript' OR "
            "(source_transcript_version_id IS NOT NULL AND source_document_version_id IS NULL "
            " AND source_note_version_id IS NULL)",
            name="ck_index_blocks_transcript_source",
        ),
        CheckConstraint(
            "source_type != 'document' OR "
            "(source_document_version_id IS NOT NULL AND source_transcript_version_id IS NULL "
            " AND source_note_version_id IS NULL)",
            name="ck_index_blocks_document_source",
        ),
        CheckConstraint(
            "source_type != 'note' OR "
            "(source_note_version_id IS NOT NULL AND source_transcript_version_id IS NULL "
            " AND source_document_version_id IS NULL)",
            name="ck_index_blocks_note_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    index_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("index_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 归属冗余列：范围内召回直接过滤，不跨课程
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 课时归属：公共课件为 NULL
    lecture_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_transcript_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_document_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_note_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # 文档块定位（block_id / 页码）
    block_id: Mapped[str | None] = mapped_column(String(200))
    page_no: Mapped[int | None] = mapped_column(Integer)
    # 转写片段范围定位（segment_start / segment_end）
    segment_start: Mapped[int | None] = mapped_column(Integer)
    segment_end: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(1024))  # 与 INDEX_EMBEDDING_DIMENSIONS 绑定
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
