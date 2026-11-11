"""Harness 升级版会议 Agent 工作流

基于现有 meeting_graph.py 升级，保留 v1 作对照。

升级点：
1. 引入 Planner 节点：分析会议类型/敏感度，动态调度 Agent
2. 套 Harness 约束：Budget / Retry / Breaker / Validator
3. Output Validator 节点：结构化校验 + 回灌重试
4. Human Review 节点：高风险会议人工审批
5. AgentRun 生命周期：节点耗时/Token/成本/Tool 调用记录

流程：
    START
      ↓
    [planner] → 规划执行计划
      ↓
    [budget_check] → 文本压缩 + 预算检查
      ↓
    [summary_agent / action_items_agent / risks_agent]（动态 fan-out，套 Harness）
      ↓
    [output_validator] → 校验 + 回灌重试
      ↓
    [human_review] → 高风险审批
      ↓
    [persist] → 落库
      ↓
    END
"""

import logging
import operator
from typing import Annotated, TypedDict, Optional

from langgraph.graph import StateGraph, END

from app.agents.meeting_graph import (
    summary_agent as _summary_agent,
    action_items_agent as _action_items_agent,
    risks_agent as _risks_agent,
)
from app.agents.harness import harness_wrap
from app.agents.nodes.planner import planner_node
from app.agents.nodes.budget_check import budget_check_node
from app.agents.nodes.output_validator import output_validator_node
from app.agents.nodes.human_review import human_review_node
from app.agents.nodes.decision_extractor import decision_extractor_node

logger = logging.getLogger(__name__)


# ── 升级版状态定义 ──

class MeetingAgentStateV2(TypedDict):
    """Harness 版 Agent 状态"""
    # 基础字段（兼容 v1）
    meeting_id: str
    meeting_title: str
    meeting_date: Optional[str]  # 会议开始时间（ISO 字符串），用于决策 decided_at
    transcript_text: str
    summary: str
    key_points: list[str]
    action_items: list[dict]
    risks: list[dict]
    errors: Annotated[list[str], operator.add]

    # Harness 新增字段
    # 注：agent_run_id / budget_guard 通过 contextvar 传递，不放入 state
    plan: dict
    transcript_compressed: bool
    # 节点重试计数
    summary_agent_retry: int
    action_items_agent_retry: int
    risks_agent_retry: int
    # Validator / Review 信号
    valid: bool
    retry_node: Optional[str]
    retry_reason: Optional[str]
    paused: bool
    approved: bool
    review_status: Optional[str]
    persisted: bool
    review_summary_preview: str
    review_risks_count: int
    review_action_items_count: int
    budget_exceeded: bool
    validation_failed: bool
    transcript_compressed_text: str
    # 评审决策抽取（Q8 决策：新增并行节点）
    decisions: list[dict]


# ── 用 harness_wrap 包裹现有 Agent（零侵入） ──

summary_agent_harnessed = harness_wrap(
    node_name="summary_agent",
    timeout=60.0,
)(_summary_agent)

action_items_agent_harnessed = harness_wrap(
    node_name="action_items_agent",
    timeout=60.0,
)(_action_items_agent)

risks_agent_harnessed = harness_wrap(
    node_name="risks_agent",
    timeout=60.0,
)(_risks_agent)

decision_extractor_harnessed = harness_wrap(
    node_name="decision_extractor",
    timeout=120.0,  # 两步流水线（detect + extract），给足时间
    validate_output=False,  # ExtractedDecision 内部已 Pydantic 校验
)(decision_extractor_node)


# ── 动态编排：根据 Planner 决定跑哪些 Agent ──

def route_after_budget_check(state) -> list[str]:
    """Planner 之后动态 fan-out 到选中的 Agent"""
    plan = state.get("plan", {})
    nodes = []
    if plan.get("should_run_summary", True):
        nodes.append("summary_agent")
    if plan.get("should_run_actions", True):
        nodes.append("action_items_agent")
    if plan.get("should_run_risks", True):
        nodes.append("risks_agent")
    if plan.get("should_run_decisions", True):  # Q8 决策：新增并行节点
        nodes.append("decision_extractor")
    # 至少跑一个，兜底
    if not nodes:
        nodes.append("summary_agent")
    logger.info(f"[Router] 选中节点: {nodes}")
    return nodes


def route_after_validator(state) -> str:
    """Validator 之后：回灌重试 / 人工审批 / 完成"""
    # 暂停信号（优先级最高）
    if state.get("paused"):
        logger.info("[Router] validator → END (paused)")
        return END  # 工作流终止，由 API 审批后重新触发

    # 需要回灌重试
    retry_node = state.get("retry_node")
    valid = state.get("valid", True)
    if not valid and retry_node:
        logger.info(f"[Router] validator → {retry_node} (retry)")
        return retry_node

    logger.info(f"[Router] validator → human_review (valid={valid}, retry_node={retry_node!r})")
    return "human_review"


# ── 持久化节点 ──

async def persist_node(state: dict) -> dict:
    """持久化节点（实际落库由 summary_service 处理，这里只标记完成）"""
    logger.info(f"[Persist] Agent Run 完成，meeting={state.get('meeting_id')}")
    return {"persisted": True}


# ── 构建升级版图 ──

def build_harnessed_meeting_graph():
    """构建带 Harness 约束的会议 Agent 工作流"""
    workflow = StateGraph(MeetingAgentStateV2)

    # 添加节点
    workflow.add_node("planner", planner_node)
    workflow.add_node("budget_check", budget_check_node)
    workflow.add_node("summary_agent", summary_agent_harnessed)
    workflow.add_node("action_items_agent", action_items_agent_harnessed)
    workflow.add_node("risks_agent", risks_agent_harnessed)
    workflow.add_node("decision_extractor", decision_extractor_harnessed)
    workflow.add_node("output_validator", output_validator_node)
    workflow.add_node("human_review", human_review_node)
    workflow.add_node("persist", persist_node)

    # 入口
    workflow.set_entry_point("planner")

    # planner → budget_check
    workflow.add_edge("planner", "budget_check")

    # budget_check → 动态 fan-out 到选中的 Agent
    workflow.add_conditional_edges("budget_check", route_after_budget_check)

    # 所有 Agent → output_validator（形成 Agent↔Validator 回灌循环）
    workflow.add_edge("summary_agent", "output_validator")
    workflow.add_edge("action_items_agent", "output_validator")
    workflow.add_edge("risks_agent", "output_validator")
    workflow.add_edge("decision_extractor", "output_validator")

    # output_validator → 动态路由（回灌重试到 retry_node / human_review / END 暂停）
    workflow.add_conditional_edges("output_validator", route_after_validator)

    # human_review → persist（通过）或 END（暂停/拒绝）
    workflow.add_conditional_edges(
        "human_review",
        lambda s: "persist" if s.get("approved") else END,
    )

    # persist → END
    workflow.add_edge("persist", END)

    return workflow.compile()


# 全局图实例
meeting_graph_v2 = build_harnessed_meeting_graph()
