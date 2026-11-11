"""统一业务错误契约

第二阶段跨模块错误响应（P02-16）：错误体固定携带
``code / message / retryable / request_id``，用于定位；不回传密钥、
服务器存储路径或原始授权凭证。

外部调用（provider）错误分类：
- 配置缺失 / 输入格式 / 结果非法 → 不可重试（retryable=False）
- 限流 / 超时 / 网络 → 可重试（retryable=True）
"""

import uuid

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class ServiceError(HTTPException):
    """携带结构化字段的业务错误，统一渲染为错误契约。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.retryable = retryable
        self.message = message
        self.retry_after_seconds = retry_after_seconds


def _request_id() -> str:
    return uuid.uuid4().hex[:12]


async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    content = {
        "code": exc.code,
        "message": exc.message,
        "retryable": exc.retryable,
        "request_id": _request_id(),
    }
    headers = {}
    if exc.retry_after_seconds is not None:
        content["retry_after_seconds"] = exc.retry_after_seconds
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=headers,
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # 未显式使用 ServiceError 的既有 404/422 也统一补齐错误契约字段
    code = "not_found" if exc.status_code == 404 else "invalid_request"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": code,
            "message": str(exc.detail),
            "retryable": False,
            "request_id": _request_id(),
        },
    )


# 错误类型常量：任务失败分类（P02-12）
ERR_CONFIG = "config_error"          # provider 未配置 / 配置无效（永久）
ERR_INPUT = "invalid_input"          # 媒体格式 / 输入非法（永久）
ERR_RATE_LIMIT = "rate_limited"      # 限流（暂时）
ERR_TIMEOUT = "timeout"              # 超时（暂时）
ERR_NO_SPEECH = "no_speech"          # 静音 / 无法识别（永久，与调用失败分型）
ERR_PROVIDER = "provider_error"      # 供应商调用失败（视类别）
ERR_INVALID_RESULT = "invalid_result"  # 结果校验失败（永久）


class ProviderUnknownError(Exception):
    """外部请求已发出但结果未知（非幂等超时/连接中断后无法确认是否执行）。

    区别于可重试的限流/明确失败：普通 retry 不得自动重发可能已收费的非幂等请求，
    须进入 provider_unknown 暂停核对（CA-07/CA-20）。
    """

    def __init__(self, message: str = "provider_unknown", code: str = "provider_unknown"):
        super().__init__(message)
        self.code = code
        self.message = message


class BudgetExhaustedError(Exception):
    """持久预算可用额度不足：停止新调用，保留 Checkpoint，retryable=False（CA-18/CA-19）。"""

    def __init__(self, message: str = "budget_exhausted"):
        super().__init__(message)
        self.code = "budget_exhausted"


class CallCancelled(Exception):
    """网关在再次外发前检测到任务已取消：停止请求，交由调用方标记 cancelled（CA-23）。

    区别于业务层的 JobCancelled/OrganizeCancelled：由统一网关在每次实际出网前校验
    ``cancel_requested`` 时抛出，使取消在重试循环内立即生效，不再发起下一次请求。
    """
