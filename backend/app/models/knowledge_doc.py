"""知识文档模型（含向量）"""

import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# pgvector 向量类型
from pgvector.sqlalchemy import Vector


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 归属主体：知识块按主体隔离，读取/检索/删除均按 owner_id 过滤
    owner_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(50))  # meeting_summary / uploaded_doc
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    embedding = mapped_column(Vector(1024))  # text-embedding-v3 维度
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
