"""Tool Registry：工具白名单 + 权限分级 + 超时 + 风险管理

会议 Agent 调用工具（检索知识库、查历史会议等）统一走注册表，
未注册的工具一律拒绝执行，防止 Agent 越权操作。

风险分级：
- READ_ONLY：只读，任意 Agent 可调
- WRITE_SAFE：安全写操作（落库纪要/行动项/风险），无需确认
- WRITE_DANGER：危险操作（删会议/发通知），需人工确认
- SYSTEM：系统级操作（导出外部），默认禁用
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"
    WRITE_SAFE = "write_safe"
    WRITE_DANGER = "write_danger"
    SYSTEM = "system"


@dataclass
class ToolSpec:
    """工具规格定义"""
    name: str
    risk: ToolRisk
    description: str
    handler: Callable[..., Awaitable[Any]]
    requires_confirmation: bool = False
    timeout_seconds: int = 30
    max_retries: int = 1


# 全局工具注册表
TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec):
    """工具注册装饰器

    用法：
        @register_tool(ToolSpec(
            name="search_knowledge",
            risk=ToolRisk.READ_ONLY,
            description="检索企业知识库",
            # handler 由装饰器自动赋值，不用传
        ))
        async def search_knowledge(query: str, top_k: int = 5):
            ...
    """
    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        spec.handler = func  # 自动把被装饰函数塞进 spec
        TOOL_REGISTRY[spec.name] = spec
        logger.info(f"[ToolRegistry] 注册工具 {spec.name} (risk={spec.risk.value})")
        return func
    return decorator


async def call_tool(
    name: str,
    agent_run_id: str = "",
    requires_confirmation_check: bool = True,
    **kwargs,
) -> dict:
    """统一工具调用入口

    流程：
    1. 校验工具是否在注册表（白名单）
    2. 危险操作检查人工确认
    3. 记录 Tool 调用到 AgentRun
    4. 带超时执行
    5. 记录结果 / 错误

    Returns:
        {"ok": bool, "result": Any, "error": str|None, "duration_ms": int}
    """
    spec = TOOL_REGISTRY.get(name)
    if not spec:
        msg = f"未注册的工具: {name}"
        logger.warning(f"[ToolRegistry] {msg}")
        return {"ok": False, "result": None, "error": msg, "duration_ms": 0}

    started_at = time.time()

    # 危险操作需要人工确认
    if requires_confirmation_check and spec.requires_confirmation:
        approved = await _check_human_approval(agent_run_id, name)
        if not approved:
            msg = f"工具 {name} 未获人工批准"
            logger.warning(f"[ToolRegistry] {msg}")
            duration_ms = int((time.time() - started_at) * 1000)
            await _record_tool_call(agent_run_id, name, kwargs, None, duration_ms, "denied", msg)
            return {"ok": False, "result": None, "error": msg, "duration_ms": duration_ms}

    # 执行（带超时 + 重试）
    last_error: Optional[Exception] = None
    for attempt in range(spec.max_retries + 1):
        try:
            result = await asyncio.wait_for(
                spec.handler(**kwargs),
                timeout=spec.timeout_seconds,
            )
            duration_ms = int((time.time() - started_at) * 1000)
            await _record_tool_call(agent_run_id, name, kwargs, result, duration_ms, "succeeded")
            return {"ok": True, "result": result, "error": None, "duration_ms": duration_ms}
        except asyncio.TimeoutError:
            last_error = TimeoutError(f"工具 {name} 超时 ({spec.timeout_seconds}s)")
            break  # 超时不重试
        except Exception as e:
            last_error = e
            if attempt >= spec.max_retries:
                break

    duration_ms = int((time.time() - started_at) * 1000)
    err_msg = str(last_error) if last_error else "未知错误"
    await _record_tool_call(agent_run_id, name, kwargs, None, duration_ms, "failed", err_msg)
    logger.warning(f"[ToolRegistry] 工具 {name} 失败: {err_msg}")
    return {"ok": False, "result": None, "error": err_msg, "duration_ms": duration_ms}


def list_tools() -> list[dict]:
    """列出所有已注册工具（供前端展示）"""
    return [
        {
            "name": spec.name,
            "risk": spec.risk.value,
            "description": spec.description,
            "requires_confirmation": spec.requires_confirmation,
            "timeout_seconds": spec.timeout_seconds,
        }
        for spec in TOOL_REGISTRY.values()
    ]


# ── 内部辅助 ──

async def _check_human_approval(run_id: str, _tool_name: str) -> bool:
    """检查危险工具是否已获人工批准

    简化实现：查询 AgentRun.review_status
    真实场景可结合工作流暂停 → 人工审批 → 恢复
    """
    if not run_id:
        return False  # 无 run_id 上下文 → 默认拒绝危险工具
    try:
        from app.services.agent_run_service import agent_run_service
        run = await agent_run_service.get_run(run_id)
        if run and run.review_status == "approved":
            return True
        return False
    except Exception:
        return False  # 查询失败保守拒绝


async def _record_tool_call(
    run_id: str,
    tool: str,
    args: dict,
    result: Any,
    duration_ms: int,
    status: str,
    error: Optional[str] = None,
) -> None:
    """记录 Tool 调用到 AgentRun"""
    if not run_id:
        return
    try:
        from app.services.agent_run_service import agent_run_service
        await agent_run_service.record_tool_call(
            run_id=run_id,
            tool=tool,
            args=args,
            result=result,
            duration_ms=duration_ms,
            status=status,
            error=error,
        )
    except Exception as e:
        logger.debug(f"记录 tool_call 失败（不影响执行）: {e}")
