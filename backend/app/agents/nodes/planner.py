"""Planner 节点：任务规划与多 Agent 动态编排

分析会议标题 + 转写文本特征，输出执行计划：
- 会议类型（standup/review/decision/brainstorm）
- 是否跑摘要 / 行动项 / 风险 Agent
- 是否需要人工审批（敏感词 + high 风险）
- 转写文本策略（full/compressed）
- 预估 Token 消耗

Planner 失败时降级为"默认全跑"，保证可用性。
"""

import logging

from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel

from app.agents.meeting_graph import get_llm, _extract_json, _invoke_with_retry
from app.agents.harness.wrap import get_run_id

logger = logging.getLogger(__name__)


class ExecutionPlan(BaseModel):
    """Planner 输出的执行计划"""
    meeting_type: str = "unknown"        # standup/review/decision/brainstorm/unknown
    should_run_summary: bool = True
    should_run_actions: bool = True
    should_run_risks: bool = True
    should_run_decisions: bool = True     # Q8 决策：评审决策抽取节点
    needs_human_review: bool = False
    transcript_strategy: str = "full"    # full/compressed
    estimated_tokens: int = 10000
    reason: str = "default"


PLANNER_PROMPT = """你是会议分析规划师。根据会议标题和转写文本特征，输出执行计划 JSON。

判断规则：
- meeting_type：standup(日报)/review(评审)/decision(决策)/brainstorm(头脑风暴)/unknown
- 转写文本字数 > 20000 → transcript_strategy="compressed"
- 识别到"裁员/离职/薪资/竞品/诉讼/合规/违规"等敏感词 → needs_human_review=true
- 纯头脑风暴会议 → should_run_risks=false（无决策无风险）
- 日报会议 → should_run_actions=false（通常无行动项）
- 日报会议 → should_run_decisions=false（通常无决策）
- 头脑风暴会议 → should_run_decisions=false（通常无决策）

只返回 JSON，格式：
{
  "meeting_type": "review",
  "should_run_summary": true,
  "should_run_actions": true,
  "should_run_risks": true,
  "should_run_decisions": true,
  "needs_human_review": false,
  "transcript_strategy": "full",
  "estimated_tokens": 8000,
  "reason": "评审会议，需完整分析"
}
"""

    # 敏感词（Planner 失败时兜底检测用）
# MVP 阶段：禁用敏感词检测，避免触发 HumanReview 阻塞端到端验证
SENSITIVE_KEYWORDS = set()  # 暂时禁用


async def planner_node(state: dict) -> dict:
    """Planner 节点：规划执行计划"""
    meeting_title = state.get("meeting_title", "")
    transcript_text = state.get("transcript_text", "")
    run_id = get_run_id()

    # 兜底规则（无论 LLM 是否成功，都要先做基础判断）
    fallback_plan = _build_fallback_plan(meeting_title, transcript_text)

    try:
        llm = get_llm()
        messages = [
            SystemMessage(content=PLANNER_PROMPT),
            HumanMessage(content=f"会议标题：{meeting_title}\n\n转写前2000字：\n{transcript_text[:2000]}"),
        ]
        content, err = await _invoke_with_retry(llm, messages, max_retries=1)
        if content is None:
            logger.warning(f"[Planner] LLM 失败，降级兜底: {err}")
            plan = fallback_plan
        else:
            parsed = _extract_json(content)
            if not isinstance(parsed, dict):
                logger.warning(f"[Planner] JSON 解析失败，降级兜底: {content[:200]}")
                plan = fallback_plan
            else:
                plan = ExecutionPlan.model_validate(parsed)
                # 合并兜底规则：LLM 可能漏掉敏感词检测
                if fallback_plan.needs_human_review:
                    plan.needs_human_review = True
    except Exception as e:
        logger.warning(f"[Planner] 异常，降级兜底: {e}")
        plan = fallback_plan

    plan_dict = plan.model_dump()
    logger.info(f"[Planner] 计划: type={plan.meeting_type} "
                f"summary={plan.should_run_summary} actions={plan.should_run_actions} "
                f"risks={plan.should_run_risks} decisions={plan.should_run_decisions} "
                f"review={plan.needs_human_review} "
                f"strategy={plan.transcript_strategy} est_tokens={plan.estimated_tokens}")

    # 持久化 plan 到 AgentRun
    if run_id:
        try:
            from app.services.agent_run_service import agent_run_service
            await agent_run_service.save_plan(run_id, plan_dict)
        except Exception as e:
            logger.debug(f"[Planner] 保存 plan 失败: {e}")

    return {"plan": plan_dict}


def _build_fallback_plan(meeting_title: str, transcript_text: str) -> ExecutionPlan:
    """基于规则的兜底执行计划（不依赖 LLM）"""
    title_lower = meeting_title.lower()

    # 敏感词检测
    needs_review = any(kw in transcript_text or kw in meeting_title for kw in SENSITIVE_KEYWORDS)

    # 会议类型推断（默认全跑）
    meeting_type = "unknown"
    should_run_summary = True
    should_run_actions = True
    should_run_risks = True
    should_run_decisions = True

    if any(kw in title_lower for kw in ["日报", "standup", "晨会", "同步"]):
        meeting_type = "standup"
        should_run_actions = False
        should_run_decisions = False  # 日报通常无决策
    elif any(kw in title_lower for kw in ["评审", "review", "评审会"]):
        meeting_type = "review"
        # 评审会议必有决策
    elif any(kw in title_lower for kw in ["决策", "decision", "决议"]):
        meeting_type = "decision"
    elif any(kw in title_lower for kw in ["头脑风暴", "brainstorm", "讨论"]):
        meeting_type = "brainstorm"
        should_run_risks = False
        should_run_decisions = False  # 头脑风暴通常无决策

    # 长文本压缩
    if len(transcript_text) > 20000:
        strategy = "compressed"
        est_tokens = min(8000, len(transcript_text) // 4)
    else:
        strategy = "full"
        est_tokens = min(15000, len(transcript_text) // 3 + 2000)

    return ExecutionPlan(
        meeting_type=meeting_type,
        should_run_summary=should_run_summary,
        should_run_actions=should_run_actions,
        should_run_risks=should_run_risks,
        should_run_decisions=should_run_decisions,
        needs_human_review=needs_review,
        transcript_strategy=strategy,
        estimated_tokens=est_tokens,
        reason=f"fallback: type={meeting_type}",
    )
