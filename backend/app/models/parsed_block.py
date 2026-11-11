"""文档解析块模型（真实定位）

解析器产出带稳定 block_id、正文与定位信息的 ParsedBlock：
- PDF 保留页码（page_no）；
- DOCX 保留段落/表格位置（paragraph_index / table_index）与标题信息（heading），不伪造页码；
- TXT / Markdown 有稳定段落定位（paragraph_index）。
block_id 在版本内稳定唯一，供证据 quote 匹配与检索来源定位使用。
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ParsedBlock(Base):
    __tablename__ = "parsed_blocks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "block_id", name="uq_parsed_block_version_block"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 版本内稳定块标识（如 p1 / p2 / para-3 / table-1 / txt-0）
    block_id: Mapped[str] = mapped_column(String(200), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # paragraph / heading / table / text
    kind: Mapped[str] = mapped_column(String(50), default="text")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # DOCX 标题信息（heading 块的层级文本或所属标题）
    heading: Mapped[str | None] = mapped_column(String(500))
    # PDF 页码（1-based）；DOCX/TXT 不伪造，为 NULL
    page_no: Mapped[int | None] = mapped_column(Integer)
    # DOCX 段落序号 / TXT·MD 段落序号
    paragraph_index: Mapped[int | None] = mapped_column(Integer)
    # DOCX 表格序号
    table_index: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document_version = relationship("SourceDocumentVersion", back_populates="blocks")
