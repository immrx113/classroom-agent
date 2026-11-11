"""纪要 API 路由"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.schemas.summary import (
    SummaryResponse,
    ActionItemResponse,
    ActionItemUpdate,
    RiskResponse,
    MeetingSummaryResponse,
    SummaryListItemResponse,
)
from app.services.access import get_accessible_lecture
from app.services.scope import ScopeContext
from app.services.summary_service import summary_service

# 全局纪要路由（不带 meeting_id 前缀）
global_router = APIRouter(prefix="/summaries", tags=["纪要管理"])

# 会议级纪要路由
router = APIRouter(prefix="/meetings/{meeting_id}", tags=["纪要管理"])


@global_router.get("", response_model=list[SummaryListItemResponse])
async def list_summaries(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """获取可访问课时下的纪要列表"""
    skip = (page - 1) * page_size
    rows, _ = await summary_service.list_summaries(db, skip=skip, limit=page_size, scope=scope)
    return [
        SummaryListItemResponse(
            id=str(s.id),
            meeting_id=str(s.meeting_id),
            meeting_title=m.title,
            content=s.content[:200] if s.content else "",
            status=s.status,
            created_at=s.created_at,
        )
        for s, m in rows
    ]


@router.post("/summarize", response_model=SummaryResponse, status_code=200)
async def generate_summary(
    meeting_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """触发 Multi-Agent 生成纪要"""
    await get_accessible_lecture(db, scope, meeting_id)

    summary = await summary_service.generate_summary(db, meeting_id)
    if not summary:
        raise HTTPException(status_code=400, detail="无转写内容，无法生成纪要")
    return summary


@router.get("/summary", response_model=MeetingSummaryResponse)
async def get_meeting_summary(
    meeting_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """获取会议纪要综合数据（纪要 + 行动项 + 风险）"""
    await get_accessible_lecture(db, scope, meeting_id)

    summary = await summary_service.get_summary(db, meeting_id)
    action_items = await summary_service.get_action_items(db, meeting_id)
    risks = await summary_service.get_risks(db, meeting_id)

    return MeetingSummaryResponse(
        summary=summary,
        action_items=action_items,
        risks=risks,
    )


@router.get("/summary/detail", response_model=SummaryResponse)
async def get_summary_detail(
    meeting_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """获取纪要详情"""
    await get_accessible_lecture(db, scope, meeting_id)
    summary = await summary_service.get_summary(db, meeting_id)
    if not summary:
        raise HTTPException(status_code=404, detail="纪要尚未生成")
    return summary


@router.get("/action-items", response_model=list[ActionItemResponse])
async def get_action_items(
    meeting_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """获取行动项列表"""
    await get_accessible_lecture(db, scope, meeting_id)
    return await summary_service.get_action_items(db, meeting_id)


@router.patch("/action-items/{item_id}", response_model=ActionItemResponse)
async def update_action_item(
    meeting_id: uuid.UUID,
    item_id: uuid.UUID,
    data: ActionItemUpdate,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """更新行动项状态"""
    await get_accessible_lecture(db, scope, meeting_id)
    from app.models.action_item import ActionItem

    result = await db.execute(
        select(ActionItem).where(
            ActionItem.id == item_id,
            ActionItem.meeting_id == meeting_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="行动项不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(item, key, value)

    await db.flush()
    await db.refresh(item)
    return item


@router.get("/risks", response_model=list[RiskResponse])
async def get_risks(
    meeting_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """获取风险列表"""
    await get_accessible_lecture(db, scope, meeting_id)
    return await summary_service.get_risks(db, meeting_id)
