"""Harness 约束系统：让 Agent 从 Demo 走向生产

包含五大模块：
- BudgetGuard：Token / 成本双闸门
- with_smart_retry：错误分类 + 指数退避 + 抖动
- CircuitBreaker：三态熔断器防雪崩
- OutputValidator：Pydantic 结构化校验 + 回灌重试
- harness_wrap：节点装饰器，零侵入包裹现有 Agent
"""

from app.agents.harness.budget import BudgetGuard, BudgetExceededError
from app.agents.harness.retry import with_smart_retry, classify_error
from app.agents.harness.circuit_breaker import CircuitBreaker
from app.agents.harness.validator import (
    validate_agent_output,
    ActionItemOut,
    RiskOut,
)
from app.agents.harness.wrap import harness_wrap, set_harness_context

__all__ = [
    "BudgetGuard",
    "BudgetExceededError",
    "with_smart_retry",
    "classify_error",
    "CircuitBreaker",
    "validate_agent_output",
    "ActionItemOut",
    "RiskOut",
    "harness_wrap",
    "set_harness_context",
]
