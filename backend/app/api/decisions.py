"""决策库 API：列表 / 搜索 / 详情

Q7 决策：MVP 只做只读 API（list / search / detail），
不做 POST/PUT/DELETE（决策由 Agent 自动抽取，不允许人工编辑）
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.services.access import get_accessible_lecture
from app.services.decision_graph_service import decision_graph_service
from app.services.scope import ScopeContext

router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.get("")
async def list_decisions(
    meeting_id: uuid.UUID | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """决策列表（分页 + 按 meeting 筛选）

    Args:
        meeting_id: 按会议筛选（可选）
        skip: 分页偏移
        limit: 每页数量（1~100）
    """
    if meeting_id:
        await get_accessible_lecture(db, scope, meeting_id)
    decisions, total = await decision_graph_service.list_decisions(
        db, meeting_id=meeting_id, skip=skip, limit=limit, scope=scope
    )
    return {
        "items": [
            {
                "id": str(d.id),
                "title": d.title,
                "chosen_option": d.chosen_option,
                "meeting_id": str(d.meeting_id) if d.meeting_id else None,
                "decided_by": d.decided_by,
                "confidence": d.confidence,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in decisions
        ],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/search")
async def search_decisions(
    q: str = Query(..., min_length=1, description="关键词或语义查询"),
    top_k: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """决策语义搜索（基于 pgvector cosine 相似度）

    适用于「为什么选 X」「X 选型决策」类查询
    """
    results = await decision_graph_service.search(db, q, top_k=top_k, scope=scope)
    return {
        "items": results,
        "query": q,
        "total": len(results),
    }


@router.get("/{decision_id}")
async def get_decision(
    decision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """决策详情（含 options + 关联决策）

    关联决策由 Q9 决策的「写入时即时向量关联」生成，
    relation_type 暂全填 'relates'（M2 再细化分类）
    """
    detail = await decision_graph_service.get_decision(db, decision_id, scope=scope)
    if not detail:
        raise HTTPException(status_code=404, detail="决策不存在")
    return detail
