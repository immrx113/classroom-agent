"""Budget Guard：Token / 成本双闸门

四道闸门：
1. 输入长度：转写文本 > 阈值时由 Planner 决定压缩策略
2. 单次 Token：LLM max_tokens 硬限制
3. 单会议总量：总 Token > max_tokens 则中止
4. 单会议成本：总成本 > max_cost_cny 则中止
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# 模型定价（CNY 元/千 token，参考阿里云 DashScope 公开价）
MODEL_PRICING = {
    "qwen-plus": {"input": 0.004, "output": 0.012},
    "qwen-turbo": {"input": 0.001, "output": 0.003},
    "qwen-max": {"input": 0.04, "output": 0.12},
}


class BudgetExceededError(Exception):
    """预算超限异常"""

    def __init__(self, kind: str, used: float, limit: float):
        self.kind = kind  # "tokens" / "cost"
        self.used = used
        self.limit = limit
        super().__init__(f"Budget {kind} exceeded: {used} > {limit}")


@dataclass
class BudgetGuard:
    """单次 Agent Run 的预算管理器

    使用方式：
        guard = BudgetGuard(run_id=run_id)
        await guard.consume("summary_agent", tokens_in=1200, tokens_out=800, model="qwen-plus")
    """

    run_id: str
    max_tokens: int = 50000
    max_cost_cny: float = 0.5
    used_tokens: int = 0
    used_cost: float = 0.0
    # 节点级明细：{node: {"tokens": int, "cost": float}}
    node_usage: dict = field(default_factory=dict)

    async def consume(
        self,
        node: str,
        tokens_in: int,
        tokens_out: int,
        model: str = None,
    ) -> None:
        """记录一次 LLM 调用的 Token 与成本消耗

        超限时抛出 BudgetExceededError，由 harness_wrap 捕获并标记节点失败。
        """
        model = model or settings.LLM_MODEL
        pricing = MODEL_PRICING.get(model, MODEL_PRICING["qwen-plus"])
        cost = (tokens_in * pricing["input"] + tokens_out * pricing["output"]) / 1000

        new_tokens = self.used_tokens + tokens_in + tokens_out
        new_cost = self.used_cost + cost

        # 闸门 1：Token 总量
        if new_tokens > self.max_tokens:
            logger.warning(
                f"[BudgetGuard] Token 超限 run={self.run_id} node={node} "
                f"{new_tokens} > {self.max_tokens}"
            )
            raise BudgetExceededError("tokens", new_tokens, self.max_tokens)

        # 闸门 2：成本
        if new_cost > self.max_cost_cny:
            logger.warning(
                f"[BudgetGuard] 成本超限 run={self.run_id} node={node} "
                f"¥{new_cost:.4f} > ¥{self.max_cost_cny}"
            )
            raise BudgetExceededError("cost", new_cost, self.max_cost_cny)

        # 提交
        self.used_tokens = new_tokens
        self.used_cost = new_cost
        node_stat = self.node_usage.setdefault(node, {"tokens": 0, "cost": 0.0})
        node_stat["tokens"] += tokens_in + tokens_out
        node_stat["cost"] += cost

        # 持久化到 AgentRun（延迟导入避免循环依赖）
        try:
            from app.services.agent_run_service import agent_run_service
            await agent_run_service.update_budget(
                run_id=self.run_id,
                used_tokens=self.used_tokens,
                used_cost=self.used_cost,
                node_usage=self.node_usage,
            )
        except Exception as e:
            logger.debug(f"BudgetGuard 持久化失败（不影响执行）: {e}")

    def summary(self) -> dict:
        """返回预算汇总（供 AgentRun 记录）"""
        return {
            "used_tokens": self.used_tokens,
            "used_cost": round(self.used_cost, 6),
            "max_tokens": self.max_tokens,
            "max_cost": self.max_cost_cny,
            "node_usage": self.node_usage,
        }
