"""转写版本与片段模型

每次处理固定 audio_asset_id，创建新的 transcript_version；
片段按 (version_id, seq_index) 唯一。起止时间统一使用从录音开头计算的整数毫秒。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    String,
    Text,
    Integer,
    BigInteger,
    DateTime,
    ForeignKey,
    func,
    UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class TranscriptVersion(Base):
    __tablename__ = "transcript_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    audio_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("audio_assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lecture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str | None] = mapped_column(String(50))
    provider_task_id: Mapped[str | None] = mapped_column(String(200))
    # building / ready / active / superseded / failed
    status: Mapped[str] = mapped_column(String(50), default="building")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transcript_versions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    segments = relationship(
        "TranscriptSegment", back_populates="version", cascade="all, delete-orphan"
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("version_id", "seq_index", name="uq_transcript_segment_version_seq"),
        CheckConstraint("start_ms IS NULL OR start_ms >= 0", name="ck_transcript_segment_start_nonneg"),
        CheckConstraint("end_ms IS NULL OR end_ms >= 0", name="ck_transcript_segment_end_nonneg"),
        CheckConstraint(
            "start_ms IS NULL OR end_ms IS NULL OR start_ms <= end_ms",
            name="ck_transcript_segment_time_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transcript_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq_index: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int | None] = mapped_column(BigInteger)
    end_ms: Mapped[int | None] = mapped_column(BigInteger)
    # 旧转写片段映射：回填时保留旧 transcripts.id，供追溯与去重报告
    source_transcript_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    version = relationship("TranscriptVersion", back_populates="segments")
