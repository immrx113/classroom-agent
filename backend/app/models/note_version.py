"""课堂笔记版本模型

笔记按版本保留历史；状态为 draft / pending / published / superseded。
发布后旧版本继续供历史引用读取；晚完成的旧输入整理不覆盖已发布的新输入笔记
（条件切换见 ``organize_service``）。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NoteVersion(Base):
    __tablename__ = "note_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lecture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transcript_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transcript_versions.id", ondelete="SET NULL")
    )
    pipeline_version: Mapped[str | None] = mapped_column(String(100))
    # draft / pending / published / superseded
    status: Mapped[str] = mapped_column(String(50), default="draft")
    # 结构化章节笔记（title / outline / sections / 关键结论 / 教师任务 / 来源版本）
    content: Mapped[dict | None] = mapped_column(JSONB)
    # 前一版本（编辑发布链上的上一版，供历史追溯）
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("note_versions.id", ondelete="SET NULL")
    )
    # 草稿编辑来源：记录编辑来源，便于追溯（不记录凭证）
    edit_source: Mapped[dict | None] = mapped_column(JSONB)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
