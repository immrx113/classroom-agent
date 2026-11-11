"""会议房间模型

多人会议房间，对应 mediasoup Router。
- 房间创建时关联到 SFU 服务（sfu_router_id）
- 会议结束后关联到 Meeting 记录（meeting_id）
"""

import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, func, ARRAY, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 关联到 Meeting（房间结束后落库的会议记录）
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id"), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 场景：tech_review / cross_align / incident_review / generic
    scene: Mapped[str] = mapped_column(String(50), default="generic")
    # 状态：active / ended / error
    status: Mapped[str] = mapped_column(String(20), default="active")
    # mediasoup Router ID（首次客户端 join 时由 SFU 创建并回填）
    sfu_router_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 参与者 display_name 列表
    participants: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    meeting = relationship("Meeting", foreign_keys="[Room.meeting_id]")
