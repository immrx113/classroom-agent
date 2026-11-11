"""Retry Policy：错误分类 + 指数退避 + 抖动

替代 meeting_graph.py 中的 _invoke_with_retry 一刀切重试。
按错误类型决定是否重试、退避多久、何时熔断。
"""

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 错误分类
RETRYABLE = {"timeout", "rate_limit", "connection", "service_unavailable", "server_error"}
NON_RETRYABLE = {"authentication", "quota_exhausted", "invalid_request", "permission"}


def classify_error(err: Exception) -> str:
    """将异常分类为错误类型字符串

    用于决定重试策略。
    """
    err_str = str(err).lower()
    err_type = type(err).__name__.lower()

    # 认证 / 权限类：不重试
    if any(kw in err_str for kw in ["authentication", "api key", "unauthorized", "forbidden"]):
        return "authentication"
    if "permission" in err_str or "accessdenied" in err_str:
        return "permission"

    # 配额耗尽：不重试（重试只会浪费配额）
    if any(kw in err_str for kw in ["freequota", "freetier", "quota exhausted", "balance"]):
        return "quota_exhausted"

    # 限流：可重试，长退避
    if "rate_limit" in err_str or "rate limit" in err_str or "429" in err_str:
        return "rate_limit"

    # 超时：可重试
    if "timeout" in err_str or "timed out" in err_str or err_type == "timeouterror":
        return "timeout"

    # 连接错误：可重试
    if any(kw in err_str for kw in ["connection", "network", "unreachable", "reset"]):
        return "connection"

    # 服务端错误（5xx）：可重试
    if any(kw in err_str for kw in ["500", "502", "503", "service unavailable", "internal server"]):
        return "service_unavailable"

    # 参数错误：不重试
    if any(kw in err_str for kw in ["invalid", "bad request", "400", "422"]):
        return "invalid_request"

    return "unknown"


async def with_smart_retry(
    func: Callable[..., Awaitable[T]],
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    timeout: float = 30.0,
    **kwargs,
) -> T:
    """带智能重试的异步函数调用

    策略：
    - 可重试错误（timeout/rate_limit/connection/5xx）：指数退避 + 抖动
    - 不可重试错误（auth/quota/invalid）：立即抛出
    - 未知错误：不重试，立即抛出（保守策略，避免无限循环）

    Args:
        func: 异步函数
        max_retries: 最大重试次数（不含首次）
        base_delay: 首次重试延迟（秒）
        max_delay: 最大延迟（秒）
        timeout: 单次调用超时（秒）
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
        except asyncio.TimeoutError as e:
            last_error = e
            err_type = "timeout"
        except Exception as e:
            last_error = e
            err_type = classify_error(e)
            if err_type == "unknown":
                # 未知错误不重试，直接抛出避免掩盖真实问题
                raise

        # 不可重试错误：立即抛出
        if err_type in NON_RETRYABLE:
            raise last_error

        # 已达最大重试次数
        if attempt >= max_retries:
            logger.error(
                f"重试 {max_retries} 次后仍失败: type={err_type} error={last_error}"
            )
            raise last_error

        # 指数退避 + 抖动（防止惊群）
        delay = min(max_delay, base_delay * (2 ** attempt))
        delay += random.uniform(0, delay * 0.1)
        logger.debug(
            f"重试 {attempt + 1}/{max_retries} type={err_type} 延迟 {delay:.2f}s"
        )
        await asyncio.sleep(delay)

    # 理论上不会走到这里
    raise last_error  # type: ignore[misc]
