"""会议场景工具实现

注册到 ToolRegistry 的具体工具：
- search_knowledge：检索企业知识库（READ_ONLY）
- get_meeting_history：查同主题历史会议（READ_ONLY）
- get_user_profile：查负责人信息（READ_ONLY）
- save_summary：写纪要（WRITE_SAFE）
- save_action_items：写行动项（WRITE_SAFE）
- save_risks：写风险（WRITE_SAFE）
- send_notification：通知行动项负责人（WRITE_DANGER，需确认）
"""

import logging
import uuid as uuid_mod
from typing import Optional

from sqlalchemy import select, func

from app.db.session import async_session_factory
from app.models.meeting import Meeting
from app.models.summary import Summary
from app.models.action_item import ActionItem
from app.models.risk import Risk
from app.agents.tools.registry import (
    ToolRisk,
    ToolSpec,
    register_tool,
)

logger = logging.getLogger(__name__)


# ── READ_ONLY 工具 ──

@register_tool(ToolSpec(
    name="search_knowledge",
    risk=ToolRisk.READ_ONLY,
    description="检索企业知识库，返回与 query 最相关的文档片段",
    handler=None,  # 装饰器会自动赋值
    timeout_seconds=15,
))
async def search_knowledge(query: str, top_k: int = 5) -> dict:
    """检索企业知识库

    Args:
        query: 查询文本
        top_k: 返回条数
    """
    from app.services.knowledge_service import knowledge_service
    async with async_session_factory() as db:
        results = await knowledge_service.search(db, query, top_k=top_k)
        return {"query": query, "results": results, "total": len(results)}


@register_tool(ToolSpec(
    name="get_meeting_history",
    risk=ToolRisk.READ_ONLY,
    description="按主题查历史会议，返回同主题历史会议纪要",
    handler=None,
    timeout_seconds=10,
))
async def get_meeting_history(topic: str, limit: int = 5) -> dict:
    """查同主题历史会议"""
    safe_topic = topic.replace('%', '\\%').replace('_', '\\_')
    async with async_session_factory() as db:
        # 模糊匹配标题
        stmt = (
            select(Meeting, Summary)
            .outerjoin(Summary, Summary.meeting_id == Meeting.id)
            .where(Meeting.title.ilike(f"%{safe_topic}%", escape='\\'))
            .where(Summary.status == "completed")
            .order_by(Meeting.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = result.all()
        return {
            "topic": topic,
            "meetings": [
                {
                    "meeting_id": str(m.id),
                    "title": m.title,
                    "summary_preview": (s.content[:200] if s and s.content else ""),
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m, s in rows
            ],
            "total": len(rows),
        }


@register_tool(ToolSpec(
    name="get_user_profile",
    risk=ToolRisk.READ_ONLY,
    description="查负责人信息，用于确认行动项 assignee 是否存在",
    handler=None,
    timeout_seconds=5,
))
async def get_user_profile(name: str) -> dict:
    """查负责人信息

    简化实现：从历史行动项中聚合该用户负责的任务
    """
    async with async_session_factory() as db:
        stmt = (
            select(
                ActionItem.assignee,
                func.count(ActionItem.id).label("total_tasks"),
                func.count(ActionItem.id).filter(ActionItem.status == "completed").label("done_tasks"),
            )
            .where(ActionItem.assignee == name)
            .group_by(ActionItem.assignee)
        )
        result = await db.execute(stmt)
        row = result.first()
        if not row:
            return {"name": name, "exists": False}
        return {
            "name": row.assignee,
            "exists": True,
            "total_tasks": row.total_tasks,
            "done_tasks": row.done_tasks,
            "completion_rate": (row.done_tasks / row.total_tasks) if row.total_tasks else 0,
        }


# ── WRITE_SAFE 工具 ──

@register_tool(ToolSpec(
    name="save_summary",
    risk=ToolRisk.WRITE_SAFE,
    description="保存会议纪要到数据库",
    handler=None,
    timeout_seconds=10,
))
async def save_summary(meeting_id: str, content: str, key_points: Optional[list] = None) -> dict:
    """保存纪要"""
    try:
        mid = uuid_mod.UUID(meeting_id)
    except ValueError:
        return {"ok": False, "error": "meeting_id 格式错误"}
    async with async_session_factory() as db:
        # 检查是否已存在
        existing = await db.execute(
            select(Summary).where(Summary.meeting_id == mid)
        )
        summary = existing.scalar_one_or_none()
        if summary:
            summary.content = content
            summary.key_points = key_points or []
            summary.status = "completed"
        else:
            summary = Summary(
                meeting_id=mid,
                content=content,
                key_points=key_points or [],
                status="completed",
            )
            db.add(summary)
        await db.commit()
        return {"summary_id": str(summary.id), "saved": True}


@register_tool(ToolSpec(
    name="save_action_items",
    risk=ToolRisk.WRITE_SAFE,
    description="保存行动项列表",
    handler=None,
    timeout_seconds=10,
))
async def save_action_items(meeting_id: str, items: list[dict]) -> dict:
    """保存行动项"""
    try:
        mid = uuid_mod.UUID(meeting_id)
    except ValueError:
        return {"ok": False, "error": "meeting_id 格式错误"}
    async with async_session_factory() as db:
        for item in items:
            action = ActionItem(
                meeting_id=mid,
                title=item.get("title", ""),
                assignee=item.get("assignee"),
                priority=item.get("priority", "medium"),
                status="pending",
            )
            db.add(action)
        await db.commit()
        return {"saved": len(items)}


@register_tool(ToolSpec(
    name="save_risks",
    risk=ToolRisk.WRITE_SAFE,
    description="保存风险列表",
    handler=None,
    timeout_seconds=10,
))
async def save_risks(meeting_id: str, risks: list[dict]) -> dict:
    """保存风险"""
    try:
        mid = uuid_mod.UUID(meeting_id)
    except ValueError:
        return {"ok": False, "error": "meeting_id 格式错误"}
    async with async_session_factory() as db:
        for r in risks:
            risk = Risk(
                meeting_id=mid,
                description=r.get("description", ""),
                severity=r.get("severity", "medium"),
                mitigation=r.get("mitigation"),
            )
            db.add(risk)
        await db.commit()
        return {"saved": len(risks)}


# ── WRITE_DANGER 工具（需人工确认） ──

@register_tool(ToolSpec(
    name="send_notification",
    risk=ToolRisk.WRITE_DANGER,
    description="通知行动项负责人（邮件/IM），需人工确认后执行",
    handler=None,
    requires_confirmation=True,
    timeout_seconds=15,
))
async def send_notification(assignee: str, message: str, channel: str = "email") -> dict:
    """发送通知（模拟实现）

    生产环境可对接企业 IM / 邮件系统。
    """
    logger.info(f"[Notification] 通知 {assignee} via {channel}: {message[:50]}...")
    # 模拟发送
    return {
        "assignee": assignee,
        "channel": channel,
        "sent": True,
        "message_preview": message[:100],
    }
