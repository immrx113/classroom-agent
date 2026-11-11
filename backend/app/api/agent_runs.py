"""Agent Run API 路由

提供 Agent 运行记录的查询、审批、工具列表等接口。
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.services.agent_run_service import agent_run_service
from app.agents.tools.registry import list_tools

router = APIRouter(prefix="/agent-runs", tags=["Agent 运行管理"])


class ReviewRequest(BaseModel):
    """人工审批请求"""
    reviewer: str = "anonymous"
    note: str | None = None
    action: Literal["approve", "reject"] = "approve"


@router.get("")
async def list_agent_runs(
    meeting_id: str | None = Query(None, description="按会议 ID 过滤"),
    status: str | None = Query(None, description="按状态过滤: pending/running/paused/succeeded/failed/cancelled"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """获取 Agent Run 列表"""
    skip = (page - 1) * page_size
    runs, total = await agent_run_service.list_runs(
        db, meeting_id=meeting_id, status=status, skip=skip, limit=page_size
    )
    return {
        "items": [r.to_dict() for r in runs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/tools/list")
async def get_tools():
    """列出所有已注册工具（供前端展示 Tool Registry）"""
    return {"tools": list_tools()}


@router.get("/stats/overview")
async def get_stats_overview(db: AsyncSession = Depends(get_db)):
    """Agent 运行统计概览（Dashboard 用）"""
    from sqlalchemy import select, func
    from app.models.agent_run import AgentRun

    # 按状态统计
    status_stmt = (
        select(AgentRun.status, func.count(AgentRun.id))
        .group_by(AgentRun.status)
    )
    status_result = await db.execute(status_stmt)
    status_counts = {row[0]: row[1] for row in status_result.all()}

    # 总 Token 与成本
    totals_stmt = select(
        func.sum(AgentRun.total_tokens).label("total_tokens"),
        func.sum(AgentRun.total_cost_usd).label("total_cost"),
        func.count(AgentRun.id).label("total_runs"),
    )
    totals_result = await db.execute(totals_stmt)
    totals = totals_result.first()

    return {
        "status_counts": status_counts,
        "total_runs": totals.total_runs or 0,
        "total_tokens": totals.total_tokens or 0,
        "total_cost_usd": round(float(totals.total_cost or 0), 6),
        "success_rate": (
            status_counts.get("succeeded", 0) / totals.total_runs
            if totals.total_runs else 0
        ),
    }


@router.get("/{run_id}")
async def get_agent_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """获取单个 Agent Run 详情"""
    run = await agent_run_service.get_run(run_id, db=db)
    if not run:
        raise HTTPException(status_code=404, detail="Agent Run 不存在")
    return run.to_dict()


@router.post("/{run_id}/review")
async def review_agent_run(run_id: str, req: ReviewRequest, db: AsyncSession = Depends(get_db)):
    """人工审批 Agent Run

    - approve：通过，恢复执行
    - reject：拒绝，终止 Run
    """
    run = await agent_run_service.get_run(run_id, db=db)
    if not run:
        raise HTTPException(status_code=404, detail="Agent Run 不存在")

    if run.review_status != "pending":
        raise HTTPException(status_code=400, detail=f"当前审批状态: {run.review_status}，无法重复审批")

    if req.action == "approve":
        updated = await agent_run_service.approve_run(run_id, req.reviewer, req.note, db=db)
        return {"status": "approved", "run": updated.to_dict() if updated else None}
    else:
        updated = await agent_run_service.reject_run(run_id, req.reviewer, req.note, db=db)
        return {"status": "rejected", "run": updated.to_dict() if updated else None}
