"""AgentRun 模型：记录 Agent 运行全生命周期

每次 Multi-Agent 工作流执行（一次会议纪要生成）对应一条 AgentRun 记录。
记录：节点执行明细、Token 消耗、成本、Tool 调用、错误、人工审批等。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, Float, Integer, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AgentRun(Base):
    """Agent 运行记录

    生命周期状态：
        pending → running → paused（等人工审批）→ running → succeeded
                → failed
                → cancelled
    """
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    graph_name: Mapped[str] = mapped_column(String(100), default="meeting_summary_graph_v2")
    status: Mapped[str] = mapped_column(String(50), default="pending", index=True)
    current_node: Mapped[str | None] = mapped_column(String(100))

    # Planner 输出的执行计划
    plan: Mapped[dict | None] = mapped_column(JSONB)

    # 时间
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Token 与成本
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # 预算上限
    max_tokens: Mapped[int] = mapped_column(Integer, default=50000)
    max_cost_usd: Mapped[float] = mapped_column(Float, default=0.5)

    # 节点级明细：[{node, status, started_at, finished_at, duration_ms, tokens, cost, error}]
    steps: Mapped[list] = mapped_column(JSONB, default=list)
    # 节点级 Token 明细：{node: {"tokens": int, "cost": float}}
    node_usage: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Tool 调用记录：[{tool, args, result, duration_ms, status, timestamp}]
    tool_calls: Mapped[list] = mapped_column(JSONB, default=list)

    # 错误信息
    error: Mapped[str | None] = mapped_column(Text)

    # LangGraph checkpoint 引用（断点恢复）
    thread_id: Mapped[str | None] = mapped_column(String(100))
    checkpoint_id: Mapped[str | None] = mapped_column(String(200))

    # 人工审批
    review_status: Mapped[str | None] = mapped_column(String(50))  # pending/approved/rejected
    reviewer: Mapped[str | None] = mapped_column(String(100))
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # 关系
    meeting = relationship("Meeting", backref="agent_runs")

    def to_dict(self) -> dict:
        """转 dict（API 返回用）"""
        return {
            "id": self.id,
            "meeting_id": str(self.meeting_id),
            "graph_name": self.graph_name,
            "status": self.status,
            "current_node": self.current_node,
            "plan": self.plan,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
            "steps": self.steps or [],
            "node_usage": self.node_usage or {},
            "tool_calls": self.tool_calls or [],
            "error": self.error,
            "review_status": self.review_status,
            "reviewer": self.reviewer,
            "review_note": self.review_note,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
        }
