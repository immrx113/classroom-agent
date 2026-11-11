"""Circuit Breaker：三态熔断器防雪崩

状态机：CLOSED → OPEN → HALF_OPEN → CLOSED/OPEN

- CLOSED：正常放行，记录失败次数
- OPEN：拒绝所有请求，等待 recovery_timeout
- HALF_OPEN：放行一次试探请求，成功→CLOSED，失败→OPEN

用途：当 LLM 服务持续不可用时，快速失败避免请求堆积导致数据库连接池耗尽。
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """三态熔断器

    使用示例：
        breaker = CircuitBreaker(fail_threshold=5, recovery_timeout=60)
        if breaker.allow():
            try:
                result = await call_llm()
                breaker.record_success()
            except Exception:
                breaker.record_failure()
                raise
        else:
            raise CircuitOpenError("熔断中，请稍后重试")
    """

    def __init__(
        self,
        name: str = "default",
        fail_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self.name = name
        self.state: str = "closed"  # closed / open / half_open
        self.fail_count: int = 0
        self.fail_threshold = fail_threshold
        self.recovery_timeout = recovery_timeout
        self.opened_at: float = 0.0
        self.last_error: Optional[str] = None

    def allow(self) -> bool:
        """是否允许放行请求"""
        if self.state == "closed":
            return True

        if self.state == "open":
            # 检查是否到了恢复时间
            if time.time() - self.opened_at > self.recovery_timeout:
                logger.info(
                    f"[CircuitBreaker:{self.name}] OPEN → HALF_OPEN "
                    f"(恢复期试探)"
                )
                self.state = "half_open"
                return True
            return False

        # half_open：仅放行首个试探请求，后续拒绝
        if self.state == "half_open":
            self.state = "open"  # 立即转回 open，防止并发多请求同时通过
            return True
        return True

    def record_success(self) -> None:
        """记录成功：重置计数器，状态恢复 closed"""
        if self.state == "half_open":
            logger.info(f"[CircuitBreaker:{self.name}] HALF_OPEN → CLOSED (恢复)")
        self.fail_count = 0
        self.state = "closed"
        self.last_error = None

    def record_failure(self, error: Optional[str] = None) -> None:
        """记录失败：累加计数，超阈值则熔断"""
        self.fail_count += 1
        self.last_error = error

        if self.state == "half_open":
            # 半开状态下失败：立即重新熔断
            logger.warning(
                f"[CircuitBreaker:{self.name}] HALF_OPEN → OPEN "
                f"(试探失败: {error})"
            )
            self.state = "open"
            self.opened_at = time.time()
            return

        if self.fail_count >= self.fail_threshold:
            logger.warning(
                f"[CircuitBreaker:{self.name}] CLOSED → OPEN "
                f"(失败 {self.fail_count}/{self.fail_threshold}: {error})"
            )
            self.state = "open"
            self.opened_at = time.time()

    def status(self) -> dict:
        """返回熔断器状态（供可观测）"""
        return {
            "name": self.name,
            "state": self.state,
            "fail_count": self.fail_count,
            "fail_threshold": self.fail_threshold,
            "last_error": self.last_error,
        }


# 全局熔断器：LLM 调用共享
llm_breaker = CircuitBreaker(name="llm", fail_threshold=5, recovery_timeout=60.0)
