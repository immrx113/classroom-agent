"""全课整理（第三阶段）模型

- ``chunk_manifests``：一次整理任务的分块清单（输入片段、预算、分块数）。
- ``chunk_items``：清单中的单个块，记录核心/上下文范围、执行状态与输出，支持逐块重试与恢复。
- ``index_intents``：笔记发布后的待索引交接意向（幂等，不冒充索引完成）。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    String,
    Integer,
    DateTime,
    ForeignKey,
    func,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChunkManifest(Base):
    __tablename__ = "chunk_manifests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    lecture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    transcript_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transcript_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 全量输入片段序号（有序），核心覆盖并集 = 该列表
    input_segment_ids: Mapped[list | None] = mapped_column(JSONB)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # 绑定配置摘要：预算 / 提示 / 输出预留 / 重叠策略 / 重试上限
    config: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChunkItem(Base):
    __tablename__ = "chunk_items"
    __table_args__ = (
        UniqueConstraint("manifest_id", "chunk_index", name="uq_chunk_item_manifest_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    manifest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chunk_manifests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 核心片段序号（本块独有，与其他块核心并集覆盖全量输入）
    core_segments: Mapped[list | None] = mapped_column(JSONB)
    # 上下文片段序号（相邻块重叠，仅用于消歧，不计入核心覆盖）
    context_segments: Mapped[list | None] = mapped_column(JSONB)
    # pending / running / succeeded / failed
    status: Mapped[str] = mapped_column(String(50), default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    # 模型输出（结构化抽取结果，含引用锚点）
    output: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    # 本块首次实际外发/重试的起止时间（A6 运行详情：逐块耗时，非网关路径无 ProviderCall 也可查）
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IndexIntent(Base):
    __tablename__ = "index_intents"
    __table_args__ = (
        UniqueConstraint("note_version_id", name="uq_index_intents_note_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    note_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("note_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 来源版本（转写版本），供索引消费者定位
    source_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transcript_versions.id", ondelete="SET NULL")
    )
    # pending / dispatched / consumed / failed
    status: Mapped[str] = mapped_column(String(50), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
