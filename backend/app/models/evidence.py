"""证据模型

evidence_spans 指向可验证来源：录音来源必须绑定转写版本与片段范围，
文档来源必须绑定文档版本与 block_id；两者互斥（CheckConstraint 约束）。
artifact_evidence 建立产物与证据的多对多关联。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    String,
    Text,
    Integer,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    func,
    UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 产物关联白名单（对应 R7）：一个产物可引用多个证据来源。
# knowledge_point / worked_example / learning_task 由模块 04 建表后启用。
ARTIFACT_TYPES = (
    "note_version",
    "knowledge_point",
    "worked_example",
    "learning_task",
    "decision",
    "summary",
)
_ARTIFACT_TYPE_IN = ", ".join(f"'{t}'" for t in ARTIFACT_TYPES)


class EvidenceSpan(Base):
    __tablename__ = "evidence_spans"
    __table_args__ = (
        # 合法证据必须且只能有单一来源；隔离证据（is_valid=FALSE）可无来源
        CheckConstraint(
            "(is_valid = FALSE) OR "
            "((transcript_version_id IS NOT NULL) != (document_version_id IS NOT NULL))",
            name="ck_evidence_span_single_source",
        ),
        # 录音来源必须带片段定位；文档来源必须带 block_id
        CheckConstraint(
            "transcript_version_id IS NULL OR "
            "(segment_start IS NOT NULL AND segment_end IS NOT NULL "
            " AND segment_start <= segment_end)",
            name="ck_evidence_span_transcript_span",
        ),
        CheckConstraint(
            "document_version_id IS NULL OR block_id IS NOT NULL",
            name="ck_evidence_span_document_block",
        ),
        # 时间范围合法（非负、不倒置）
        CheckConstraint("start_ms IS NULL OR start_ms >= 0", name="ck_evidence_span_start_nonneg"),
        CheckConstraint("end_ms IS NULL OR end_ms >= 0", name="ck_evidence_span_end_nonneg"),
        CheckConstraint(
            "start_ms IS NULL OR end_ms IS NULL OR start_ms <= end_ms",
            name="ck_evidence_span_time_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 录音来源：绑定转写版本与片段范围
    transcript_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transcript_versions.id", ondelete="CASCADE")
    )
    segment_start: Mapped[int | None] = mapped_column(Integer)
    segment_end: Mapped[int | None] = mapped_column(Integer)
    start_ms: Mapped[int | None] = mapped_column(BigInteger)
    end_ms: Mapped[int | None] = mapped_column(BigInteger)
    # 文档来源：绑定文档版本与 block_id
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_document_versions.id", ondelete="CASCADE"),
    )
    block_id: Mapped[str | None] = mapped_column(String(200))
    page_no: Mapped[int | None] = mapped_column(Integer)
    # 引用原文与有效性
    quote: Mapped[str | None] = mapped_column(Text)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ArtifactEvidence(Base):
    __tablename__ = "artifact_evidence"
    __table_args__ = (
        UniqueConstraint(
            "artifact_type", "artifact_id", "evidence_id", name="uq_artifact_evidence"
        ),
        CheckConstraint(
            f"artifact_type IN ({_ARTIFACT_TYPE_IN})",
            name="ck_artifact_evidence_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evidence_spans.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
