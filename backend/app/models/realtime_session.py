"""实时会议会话模型

记录一次实时会议的元信息与状态。
- meeting_id 关联 Meeting（实时会议结束时落库为 Meeting 记录）
- status 状态机：active(进行中) / ended(已结束) / error(异常)
- participants 当前在线参与者（WebSocket 连接数）
"""

import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RealtimeSession(Base):
    __tablename__ = "realtime_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 关联 Meeting：实时会议结束时创建/关联 Meeting 记录
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active/ended/error
    # 当前在线参与者数（WebSocket 连接数）
    participants: Mapped[int] = mapped_column(Integer, default=0)
    # 累计转写片段数（用于 seq_index 递增）
    segment_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
