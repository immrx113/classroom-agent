"""Budget Check 节点：预算检查 + 文本压缩

根据 Planner 输出的策略：
- compressed：对超长转写文本做地图式摘要
- 检查预估 Token 是否超预算
"""

import logging

from app.agents.meeting_graph import get_llm, _invoke_with_retry
from langchain_core.messages import SystemMessage, HumanMessage

logger = logging.getLogger(__name__)

COMPRESS_PROMPT = """你是会议文本压缩助手。将下面的转写文本压缩为地图式摘要，保留：
1. 所有发言人及其主要观点
2. 关键决策与行动项
3. 重要数字、日期、人名

压缩后字数控制在原文的 30% 以内，但不要丢失关键信息。
直接输出压缩文本，不要加任何说明。
"""


async def budget_check_node(state: dict) -> dict:
    """预算检查 + 文本压缩"""
    plan = state.get("plan", {})
    strategy = plan.get("transcript_strategy", "full")
    transcript_text = state.get("transcript_text", "")

    if strategy == "compressed" and len(transcript_text) > 20000:
        logger.info(f"[BudgetCheck] 压缩文本 {len(transcript_text)} → 估计 30%")
        try:
            llm = get_llm()
            messages = [
                SystemMessage(content=COMPRESS_PROMPT),
                HumanMessage(content=transcript_text),
            ]
            content, err = await _invoke_with_retry(llm, messages, max_retries=1)
            if content and len(content) > 100:
                logger.info(f"[BudgetCheck] 压缩完成: {len(content)} 字符")
                return {"transcript_compressed_text": content, "transcript_compressed": True}
            else:
                logger.warning(f"[BudgetCheck] 压缩失败，用原文: {err}")
        except Exception as e:
            logger.warning(f"[BudgetCheck] 压缩异常，用原文: {e}")

    return {"transcript_compressed": False}
