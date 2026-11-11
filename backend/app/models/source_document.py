"""课件源文档与版本模型

文件级身份独立于文件名：同名文件通过 source_document 稳定 ID 区分。
lecture_id 为空表示课程公共课件。版本记录解析产物身份与解析状态；
解析块见 ``parsed_block``（真实定位），索引块见 ``index_version``。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class SourceDocument(Base):
    __tablename__ = "source_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lecture_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 原始文件名（展示用，与 title 身份无关；同名不合并）
    filename: Mapped[str | None] = mapped_column(String(500))
    storage_key: Mapped[str | None] = mapped_column(String(500))
    mime: Mapped[str | None] = mapped_column(String(100))
    # 原始字节摘要（文件身份去重/校验，非标题）
    content_hash: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    versions = relationship(
        "SourceDocumentVersion", back_populates="document", cascade="all, delete-orphan"
    )


class SourceDocumentVersion(Base):
    __tablename__ = "source_document_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    # building / ready / active / superseded / failed
    status: Mapped[str] = mapped_column(String(50), default="building")
    # 解析产物状态：parsed / needs_ocr / unsupported / failed（与 status 分型，
    # 空文本与不支持格式不伪装“解析/索引成功”，也不混作“无相关内容”）
    parse_status: Mapped[str] = mapped_column(String(50), default="parsed")
    error_code: Mapped[str | None] = mapped_column(String(100))
    # 解析后的正文摘要（内容身份），与 parser/chunker 配置绑定
    content_hash: Mapped[str | None] = mapped_column(String(128))
    config: Mapped[dict | None] = mapped_column(JSONB)
    block_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    document = relationship("SourceDocument", back_populates="versions")
    blocks = relationship(
        "ParsedBlock", back_populates="document_version", cascade="all, delete-orphan"
    )
