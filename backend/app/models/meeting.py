"""会议 / 课时模型

兼容期：本表继续沿用 meetings 物理表，作为「课时(Lecture)」的存储位置。
对外接口统一使用 lecture 语义；旧会议数据通过 course_id + legacy_meeting 归入历史课程。
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    String,
    Text,
    DateTime,
    Date,
    Boolean,
    ForeignKey,
    func,
    ARRAY,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 课程归属：旧会议迁移前为空，回填后指向历史课程
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="SET NULL"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    audio_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(50), default="pending")
    # 转写模式：real(真实API) / mock(本地降级) / NULL(未转写)
    transcription_mode: Mapped[str | None] = mapped_column(String(20), default=None)
    # 旧会议导入的历史数据标记，不自动宣称已是课堂
    legacy_meeting: Mapped[bool] = mapped_column(Boolean, default=False)
    # 课时日期（区别于 created_at；旧数据可为空）
    lecture_date: Mapped[date | None] = mapped_column(Date)
    # 当前有效音频资产 / 转写版本（由阶段任务原子切换，服务层校验，不设外键避免环）
    active_audio_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    active_transcript_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    # 当前有效（已发布）课堂笔记版本（由整理任务/发布条件切换，服务层校验）
    active_note_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    participants: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # 关系
    course = relationship("Course", back_populates="lectures")
    transcripts = relationship("Transcript", back_populates="meeting", cascade="all, delete-orphan")
    summary = relationship("Summary", back_populates="meeting", cascade="all, delete-orphan", uselist=False)
    action_items = relationship("ActionItem", back_populates="meeting", cascade="all, delete-orphan")
    risks = relationship("Risk", back_populates="meeting", cascade="all, delete-orphan")
