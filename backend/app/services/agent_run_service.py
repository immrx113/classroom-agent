"""AgentRun 生命周期管理服务

负责 AgentRun 的创建、状态流转、节点 step 记录、预算更新、Tool 调用记录、人工审批等。
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import async_session_factory
from app.models.agent_run import AgentRun

logger = logging.getLogger(__name__)


class AgentRunService:
    """AgentRun 生命周期管理"""

    # ── 创建与状态流转 ──

    async def create_run(
        self,
        meeting_id: str,
        graph_name: str = "meeting_summary_graph_v2",
        max_tokens: int = 50000,
        max_cost_usd: float = 0.5,
    ) -> AgentRun:
        """创建新的 AgentRun 记录"""
        async with async_session_factory() as db:
            run = AgentRun(
                meeting_id=meeting_id,
                graph_name=graph_name,
                status="pending",
                max_tokens=max_tokens,
                max_cost_usd=max_cost_usd,
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)
            return run

    async def start_run(self, run_id: str, thread_id: Optional[str] = None) -> None:
        """标记 Run 为 running"""
        async with async_session_factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    status="running",
                    started_at=datetime.now(timezone.utc),
                    thread_id=thread_id,
                )
            )
            await db.commit()

    async def finish_run(
        self,
        run_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """标记 Run 完成（succeeded / failed / cancelled）"""
        async with async_session_factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    status=status,
                    finished_at=datetime.now(timezone.utc),
                    error=error,
                )
            )
            await db.commit()

    async def pause_run(self, run_id: str, current_node: str = "human_review") -> None:
        """暂停 Run（等待人工审批）"""
        async with async_session_factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(status="paused", current_node=current_node, review_status="pending")
            )
            await db.commit()

    async def set_current_node(self, run_id: str, node: str) -> None:
        """更新当前节点"""
        async with async_session_factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(current_node=node)
            )
            await db.commit()

    async def save_plan(self, run_id: str, plan: dict) -> None:
        """保存 Planner 输出的执行计划"""
        async with async_session_factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(plan=plan)
            )
            await db.commit()

    # ── 节点 step 记录 ──

    async def record_step_start(self, run_id: str, node: str) -> None:
        """记录节点开始"""
        if not run_id:
            return
        try:
            async with async_session_factory() as db:
                run = await db.get(AgentRun, run_id)
                if not run:
                    logger.warning(f"record_step_start: run_id={run_id} 不存在")
                    return
                steps = list(run.steps or [])
                steps.append({
                    "node": node,
                    "status": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                })
                # 强制标记字段变更（JSONB 默认不可变检测）
                run.steps = steps
                run.current_node = node
                flag_modified(run, "steps")
                await db.commit()
        except Exception as e:
            logger.warning(f"record_step_start 失败: {e}")

    async def record_step_end(
        self,
        run_id: str,
        node: str,
        status: str,
        duration_ms: int,
        error: Optional[str] = None,
    ) -> None:
        """记录节点结束"""
        if not run_id:
            return
        try:
            async with async_session_factory() as db:
                run = await db.get(AgentRun, run_id)
                if not run:
                    return
                steps = list(run.steps or [])
                # 找到最后一个同名节点（支持重试）
                for step in reversed(steps):
                    if step.get("node") == node and step.get("status") == "running":
                        step["status"] = status
                        step["finished_at"] = datetime.now(timezone.utc).isoformat()
                        step["duration_ms"] = duration_ms
                        if error:
                            step["error"] = error
                        break
                run.steps = steps
                flag_modified(run, "steps")
                await db.commit()
        except Exception as e:
            logger.warning(f"record_step_end 失败: {e}")

    # ── 预算更新 ──

    async def update_budget(
        self,
        run_id: str,
        used_tokens: int,
        used_cost: float,
        node_usage: dict,
    ) -> None:
        """更新预算消耗（由 BudgetGuard 调用）"""
        if not run_id:
            return
        try:
            async with async_session_factory() as db:
                run = await db.get(AgentRun, run_id)
                if not run:
                    return
                run.total_tokens = used_tokens
                run.total_cost_usd = used_cost
                run.node_usage = node_usage
                flag_modified(run, "node_usage")
                await db.commit()
        except Exception as e:
            logger.debug(f"update_budget 失败: {e}")

    # ── Tool 调用记录 ──

    async def record_tool_call(
        self,
        run_id: str,
        tool: str,
        args: dict,
        result: Optional[dict] = None,
        duration_ms: int = 0,
        status: str = "succeeded",
        error: Optional[str] = None,
    ) -> None:
        """记录一次 Tool 调用"""
        if not run_id:
            return
        try:
            async with async_session_factory() as db:
                run = await db.get(AgentRun, run_id)
                if not run:
                    return
                calls = run.tool_calls or []
                calls.append({
                    "tool": tool,
                    "args": args,
                    "result": result,
                    "duration_ms": duration_ms,
                    "status": status,
                    "error": error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                run.tool_calls = calls
                flag_modified(run, "tool_calls")
                await db.commit()
        except Exception as e:
            logger.debug(f"record_tool_call 失败: {e}")

    # ── 人工审批 ──

    async def approve_run(
        self,
        run_id: str,
        reviewer: str,
        note: Optional[str] = None,
        db: AsyncSession | None = None,
    ) -> Optional[AgentRun]:
        """人工审批通过"""
        close_db = db is None
        if close_db:
            db = async_session_factory()
            await db.__aenter__()
        try:
            run = await db.get(AgentRun, run_id)
            if not run:
                return None
            run.review_status = "approved"
            run.reviewer = reviewer
            run.review_note = note
            run.reviewed_at = datetime.now(timezone.utc)
            run.status = "running"  # 恢复执行
            await db.commit()
            await db.refresh(run)
            return run
        finally:
            if close_db:
                await db.__aexit__(None, None, None)

    async def reject_run(
        self,
        run_id: str,
        reviewer: str,
        note: Optional[str] = None,
        db: AsyncSession | None = None,
    ) -> Optional[AgentRun]:
        """人工审批拒绝"""
        close_db = db is None
        if close_db:
            db = async_session_factory()
            await db.__aenter__()
        try:
            run = await db.get(AgentRun, run_id)
            if not run:
                return None
            run.review_status = "rejected"
            run.reviewer = reviewer
            run.review_note = note
            run.reviewed_at = datetime.now(timezone.utc)
            run.status = "cancelled"
            run.finished_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(run)
            return run
        finally:
            if close_db:
                await db.__aexit__(None, None, None)

    # ── 查询 ──

    async def get_run(self, run_id: str, db: AsyncSession | None = None) -> Optional[AgentRun]:
        """获取单个 Run"""
        if db:
            return await db.get(AgentRun, run_id)
        async with async_session_factory() as session:
            return await session.get(AgentRun, run_id)

    async def list_runs(
        self,
        db: AsyncSession,
        meeting_id: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[AgentRun], int]:
        """获取 Run 列表"""
        stmt = select(AgentRun)
        if meeting_id:
            stmt = stmt.where(AgentRun.meeting_id == meeting_id)
        if status:
            stmt = stmt.where(AgentRun.status == status)
        stmt = stmt.order_by(AgentRun.created_at.desc()).offset(skip).limit(limit)

        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        count_stmt = select(func.count(AgentRun.id))
        if meeting_id:
            count_stmt = count_stmt.where(AgentRun.meeting_id == meeting_id)
        if status:
            count_stmt = count_stmt.where(AgentRun.status == status)
        total = (await db.execute(count_stmt)).scalar_one()

        return rows, total


# 全局实例
agent_run_service = AgentRunService()
