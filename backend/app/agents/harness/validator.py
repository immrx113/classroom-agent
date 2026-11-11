"""Output Validator：Pydantic 结构化校验 + 回灌重试

校验 Agent 输出是否符合预期结构，失败时返回错误信息供回灌重试。
"""

import logging
from typing import Optional

from pydantic import BaseModel, field_validator, ValidationError

logger = logging.getLogger(__name__)


class ActionItemOut(BaseModel):
    """行动项输出结构"""
    title: str
    assignee: Optional[str] = None
    due_date: Optional[str] = None
    priority: str

    @field_validator("priority")
    @classmethod
    def check_priority(cls, v: str) -> str:
        if v not in ("high", "medium", "low"):
            raise ValueError(f"priority 非法: {v}，应为 high/medium/low")
        return v

    @field_validator("title")
    @classmethod
    def check_title(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title 不能为空")
        return v.strip()


class RiskOut(BaseModel):
    """风险输出结构"""
    description: str
    severity: str
    mitigation: Optional[str] = None

    @field_validator("severity")
    @classmethod
    def check_severity(cls, v: str) -> str:
        if v not in ("high", "medium", "low"):
            raise ValueError(f"severity 非法: {v}，应为 high/medium/low")
        return v

    @field_validator("description")
    @classmethod
    def check_description(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("description 不能为空")
        return v.strip()


async def validate_agent_output(
    node: str,
    raw: list | dict | str | None,
) -> tuple[bool, str, Optional[list | dict | str]]:
    """校验 Agent 输出

    Args:
        node: 节点名（summary_agent / action_items_agent / risks_agent）
        raw: 原始输出

    Returns:
        (ok, error_msg, cleaned_data)
        - ok=True 时 cleaned_data 为校验后的数据
        - ok=False 时 cleaned_data 为 None，error_msg 为错误描述
    """
    if raw is None:
        return False, f"{node} 输出为 None", None

    try:
        if node == "action_items_agent":
            if not isinstance(raw, list):
                return False, f"行动项应为数组，实际类型: {type(raw).__name__}", None
            cleaned = [ActionItemOut.model_validate(x).model_dump() for x in raw]
            return True, "ok", cleaned

        if node == "risks_agent":
            if not isinstance(raw, list):
                return False, f"风险应为数组，实际类型: {type(raw).__name__}", None
            cleaned = [RiskOut.model_validate(x).model_dump() for x in raw]
            return True, "ok", cleaned

        if node == "summary_agent":
            if not isinstance(raw, str):
                return False, f"纪要应为字符串，实际类型: {type(raw).__name__}", None
            if len(raw.strip()) < 50:
                return False, f"纪要内容过短（{len(raw.strip())} 字符），疑似生成失败", None
            return True, "ok", raw.strip()

        # 未知节点：放行
        return True, "ok", raw

    except ValidationError as e:
        err_msg = f"{node} 结构校验失败: {e}"
        logger.warning(err_msg)
        return False, err_msg, None
    except Exception as e:
        err_msg = f"{node} 校验异常: {e}"
        logger.warning(err_msg)
        return False, err_msg, None
