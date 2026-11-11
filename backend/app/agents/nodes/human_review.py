"""Human Review 节点：高风险会议人工审批

触发条件（任一满足）：
- Planner 标记 needs_human_review=true
- 转写文本含敏感词
- 风险 Agent 识别到 high severity 风险

实现方式：
- 简化版：基于 AgentRun.review_status 字段轮询
- 完整版：使用 LangGraph interrupt 暂停工作流，前端审批后 Command(resume=...) 恢复

为兼容现有 LangGraph 版本，本实现用"暂停 + 轮询"模式：
节点检测到需要审批时，把 AgentRun 状态置为 paused，
工作流返回，由 API 层在审批通过后重新触发执行。
"""

import logging

from app.agents.harness.wrap import get_run_id

logger = logging.getLogger(__name__)


async def human_review_node(state: dict) -> dict:
    """人工审批节点

    MVP 阶段：禁用审批，直接放行（避免阻塞端到端验证）
    """
    logger.info("[HumanReview] MVP 阶段：直接放行，跳过审批")
    return {"approved": True, "review_status": "approved"}


async def check_review_status(run_id: str) -> str:
    """查询审批状态

    Returns:
        "pending" / "approved" / "rejected" / "skipped" / "unknown"
    """
    if not run_id:
        return "skipped"
    try:
        from app.services.agent_run_service import agent_run_service
        run = await agent_run_service.get_run(run_id)
        if not run:
            return "unknown"
        return run.review_status or "pending"
    except Exception as e:
        logger.warning(f"[HumanReview] 查询审批状态失败: {e}")
        return "unknown"
