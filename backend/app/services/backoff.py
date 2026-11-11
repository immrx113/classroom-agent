"""有界退避与外部错误分类（专项「Checkpoint + AI 调用控制」S3）

- 冻结公式（CA-21）：第 n 次失败后 ``backoff=Uniform(0, min(cap, base*2^(n-1)))``，
  ``delay=max(backoff, parsed_retry_after, local_limiter_delay)``；base=2、cap=30、
  max_attempts=3（首次算在内）。
- Retry-After 支持秒数与 HTTP-date（RFC 9110），坏值忽略并记诊断（CA-20）。
- 错误分类：ProviderUnknownError → unknown；明确不可重试（401/403/非法 schema/无语音）
  → permanent；明确限流/幂等可重试 → retryable；非幂等超时 → unknown。
"""

import logging
import random
from datetime import datetime, timezone

from app.config import settings

logger = logging.getLogger(__name__)

CLASS_RETRYABLE = "retryable"
CLASS_PERMANENT = "permanent"
CLASS_UNKNOWN = "unknown"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compute_backoff(
    attempt_no: int,
    *,
    retry_after: float | None = None,
    local_limiter_delay: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """full-jitter 退避；``attempt_no`` 为刚失败的实际请求次数（1-based）。"""
    rng = rng or random.Random()
    base = settings.CHECKPOINT_EXT_BACKOFF_BASE
    cap = settings.CHECKPOINT_EXT_BACKOFF_CAP
    upper = min(cap, base * (2 ** max(0, attempt_no - 1)))
    jitter = rng.uniform(0.0, upper)
    return max(jitter, retry_after or 0.0, local_limiter_delay or 0.0)


def parse_retry_after(value) -> float | None:
    """解析 Retry-After：秒数或 HTTP-date；坏值返回 None（忽略并记诊断，不截短）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return float(s)
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt is None:
        return None
    delta = (dt.replace(tzinfo=timezone.utc) - _now()).total_seconds()
    return max(0.0, delta)


def classify_exception(exc: Exception, *, is_idempotent: bool) -> str:
    """分类外部异常为 retryable / permanent / unknown（CA-20）。

    - 明确未执行（429/限流）→ retryable；5xx / provider_error → retryable（服务器已响应）。
    - 非幂等超时/连接中断 → unknown（无法证明未执行，CA-07）。
    - 401/403/非法 schema/无语音等 retryable=False → permanent。
    """
    from app.services.errors import (
        ERR_RATE_LIMIT,
        ERR_TIMEOUT,
        ProviderUnknownError,
    )

    if isinstance(exc, ProviderUnknownError):
        return CLASS_UNKNOWN

    retryable = getattr(exc, "retryable", None)
    if retryable is False:
        return CLASS_PERMANENT

    error_type = getattr(exc, "error_type", None) or getattr(exc, "code", None)
    if error_type in ("rate_limited", ERR_RATE_LIMIT):
        # 明确未执行（429），可安全重试
        return CLASS_RETRYABLE
    if error_type in ("timeout", ERR_TIMEOUT, "connection", "connection_error"):
        # 非幂等超时/连接中断无法证明未执行；幂等查询/提交可安全重试
        return CLASS_RETRYABLE if is_idempotent else CLASS_UNKNOWN
    # provider_error（5xx 已响应）、其余 retryable=True → retryable
    return CLASS_RETRYABLE
