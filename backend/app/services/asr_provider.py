"""ASR provider 抽象

第二阶段将「提交 → 供应商 task_id → 轮询 → 解析」拆分为可追踪阶段（P02-10）。
- 真实 provider 缺配置在提交前即失败（P02-09），绝不自动降级 Mock。
- fake 为遵循真实契约的可控替身（自动化验证专用，不冒充真实调用）。
- demo 为显式演示模式，产物标记 is_demo，不发布为正式转写。

provider 选择由 ``settings.ASR_MODE`` 决定；测试可经 ``set_provider`` 注入替身。
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings
from app.services.errors import (
    ERR_CONFIG,
    ERR_INVALID_RESULT,
    ERR_NO_SPEECH,
    ERR_PROVIDER,
    ERR_RATE_LIMIT,
    ERR_TIMEOUT,
    ProviderUnknownError,
)

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """带结构化错误类型的供应商失败，用于任务分类。"""

    def __init__(
        self, error_type: str, message: str, retryable: bool, code: str = "provider_error",
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds


@dataclass
class SubmitResult:
    task_id: str
    # 已上传源对象的永久 object_key（CA-08 持久化，供终态后清理；与进程内字典无关）
    object_key: Optional[str] = None


class ASRProvider:
    name: str = "base"

    async def submit(self, local_path: str) -> SubmitResult:
        raise NotImplementedError

    async def poll(self, task_id: str) -> list[dict]:
        raise NotImplementedError

    async def cleanup(self, object_key: str | None) -> bool:
        """终态后删除已上传源对象；返回是否成功。失败由调用方持久化待清理状态后重试。"""
        return True


# ── 可控替身（自动化验证）────────────────────────────────────────────
def _fake_segments() -> list[dict]:
    return [
        {"speaker": "说话人1", "content": "第一句话，用于验证顺序与时间。", "start_ms": 0, "end_ms": 1000},
        {"speaker": "说话人2", "content": "第二句话，继续验证。", "start_ms": 1200, "end_ms": 2200},
        {"speaker": "说话人1", "content": "第三句话，结束。", "start_ms": 2400, "end_ms": 3200},
    ]


class FakeASRProvider(ASRProvider):
    """可控替身：按注入的 submit_result / poll_result 返回受控结果，并记录调用次数。"""

    name = "fake"

    def __init__(self):
        self.submit_result: str = "success"  # success / config_missing / rate_limit / timeout / failure / unknown
        self.poll_result: str = "success"    # success / silence / incomplete / rate_limit / timeout / failure / unknown
        self.segments: list[dict] = _fake_segments()
        self.submit_calls: int = 0
        self.poll_calls: int = 0
        self.submitted_task_ids: list[str] = []
        self.submitted_object_keys: list[str] = []
        self.cleanup_calls: int = 0
        self.cleanup_fail: bool = False  # 注入清理失败，供 CA-08 重试验证
        # CA-22：pending 轮询次数（返回 None 表示仍在处理，服务继续按间隔轮询）。
        self.pending_polls: int = 0
        # CA-07：供应商已接收但结果未知（非幂等窗口）；提交/轮询各置位一次。
        self.submit_unknown: bool = False
        self.poll_unknown: bool = False

    async def submit(self, local_path: str) -> SubmitResult:
        self.submit_calls += 1
        if self.submit_unknown:
            raise ProviderUnknownError("提交已发出但结果未知")
        if self.submit_result == "config_missing":
            raise ProviderError(ERR_CONFIG, "供应商凭证未配置", retryable=False)
        if self.submit_result == "rate_limit":
            raise ProviderError(ERR_RATE_LIMIT, "限流，稍后重试", retryable=True, code="rate_limited")
        if self.submit_result == "timeout":
            raise ProviderError(ERR_TIMEOUT, "提交超时", retryable=True, code="timeout")
        if self.submit_result == "failure":
            raise ProviderError(ERR_PROVIDER, "供应商内部错误", retryable=True)
        if self.submit_result == "unknown":
            raise ProviderUnknownError("提交已发出但结果未知")
        task_id = f"fake-task-{self.submit_calls}"
        object_key = f"fake-oss-{self.submit_calls}"
        self.submitted_task_ids.append(task_id)
        self.submitted_object_keys.append(object_key)
        return SubmitResult(task_id=task_id, object_key=object_key)

    async def cleanup(self, object_key: str | None) -> bool:
        self.cleanup_calls += 1
        if self.cleanup_fail:
            return False
        return True

    async def poll(self, task_id: str) -> list[dict] | None:
        self.poll_calls += 1
        if self.poll_unknown:
            raise ProviderUnknownError("轮询已发出但结果未知")
        if self.pending_polls > 0:
            self.pending_polls -= 1
            return None  # 仍在处理，服务按间隔继续轮询（CA-22）
        if self.poll_result == "rate_limit":
            raise ProviderError(ERR_RATE_LIMIT, "轮询限流", retryable=True, code="rate_limited")
        if self.poll_result == "timeout":
            raise ProviderError(ERR_TIMEOUT, "轮询超时", retryable=True, code="timeout")
        if self.poll_result == "failure":
            raise ProviderError(ERR_PROVIDER, "供应商内部错误", retryable=True)
        if self.poll_result == "unknown":
            raise ProviderUnknownError("轮询已发出但结果未知")
        if self.poll_result == "silence":
            return []
        if self.poll_result == "incomplete":
            raise ProviderError(ERR_INVALID_RESULT, "供应商返回不完整回执", retryable=False, code="incomplete_result")
        return [dict(s) for s in self.segments]


# ── 显式演示 provider ────────────────────────────────────────────────
_DEMO_SEGMENTS = [
    {"speaker": "演示说话人", "content": "这是一条显式演示转写，仅用于演示课程。", "start_ms": 0, "end_ms": 1500},
]


class DemoASRProvider(ASRProvider):
    name = "demo"

    async def submit(self, local_path: str) -> SubmitResult:
        return SubmitResult(task_id="demo-task")

    async def poll(self, task_id: str) -> list[dict]:
        return [dict(s) for s in _DEMO_SEGMENTS]


# ── 真实 provider（DashScope）────────────────────────────────────────
class DashScopeASRProvider(ASRProvider):
    """复用既有 DashScope 接入；缺配置在提交前失败，不降级。"""

    name = "real"

    def __init__(self):
        from app.services.dashscope_asr_service import dashscope_asr_service
        from app.services.oss_service import oss_service
        self._asr = dashscope_asr_service
        self._oss = oss_service

    async def submit(self, local_path: str) -> SubmitResult:
        if not self._asr.is_available:
            raise ProviderError(ERR_CONFIG, "DashScope API Key 未配置", retryable=False)
        if not self._oss.is_available:
            raise ProviderError(ERR_CONFIG, "OSS 未配置，无法提供公网音频 URL", retryable=False)
        # The first real trial is deliberately short; validate locally before billing.
        from app.services.media_service import probe
        info = probe(local_path)
        if info.duration_ms > 120000 or info.size > 8 * 1024 * 1024:
            raise ProviderError(ERR_CONFIG, "首轮真实试用仅支持120秒内、8MB内音频", retryable=False)
        # 网关启用时预算由统一网关接管，真实 adapter 为纯 transport，不写旧 SQLite 账本（CA-15/CA-19）。
        if not settings.CALL_GATEWAY_ENABLED:
            from app.services.trial_budget import reserve_call, record_usage
            try:
                request_id = reserve_call("asr", settings.DASHSCOPE_ASR_MODEL)
            except RuntimeError as exc:
                raise ProviderError(ERR_CONFIG, str(exc), retryable=False) from exc
            record_usage(request_id, {"duration_ms": info.duration_ms, "bytes": info.size})
        # 上传并持久化 object_key（CA-08）；后续提交失败立即清理，成功后推迟到终态清理。
        object_key = self._oss.upload_audio_object(local_path)
        if not object_key:
            raise ProviderError(ERR_PROVIDER, "OSS 上传失败", retryable=True)
        url = self._oss.sign_url(object_key)
        if not url:
            self._oss.delete_object_key(object_key)
            raise ProviderError(ERR_PROVIDER, "OSS 签名失败", retryable=True)
        # 真实提交错误透传（CA-07/CA-20）：429 保留 Retry-After，401/403 永久停止，
        # 提交响应丢失（超时/连接/回执缺失）进入 unknown，阻断普通重试。
        from app.services.backoff import parse_retry_after
        from app.services.dashscope_asr_service import (
            DashScopeASRConnectionError,
            DashScopeASRHttpError,
        )

        try:
            task_id = await self._asr._submit_task(url)
        except DashScopeASRHttpError as exc:
            self._oss.delete_object_key(object_key)
            if exc.status_code == 429:
                raise ProviderError(ERR_RATE_LIMIT, "提交限流", retryable=True, code="rate_limited",
                                    retry_after_seconds=parse_retry_after(exc.retry_after)) from exc
            if exc.status_code >= 500:
                raise ProviderError(ERR_PROVIDER, f"提交HTTP {exc.status_code}", retryable=True) from exc
            raise ProviderError(
                ERR_PROVIDER, f"提交HTTP {exc.status_code}", retryable=False,
                code="invalid_auth" if exc.status_code in (401, 403) else "invalid_request",
            ) from exc
        except DashScopeASRConnectionError as exc:
            self._oss.delete_object_key(object_key)
            raise ProviderError(ERR_TIMEOUT, "提交响应丢失", retryable=True, code="timeout") from exc
        return SubmitResult(task_id=task_id, object_key=object_key)

    async def poll(self, task_id: str) -> list[dict] | None:
        # 单次查询 + 结果解析（CA-15）：不再内部循环轮询，pending 返回 None，由服务按间隔继续。
        # 真实 HTTP 错误保留 status/Retry-After，供网关分类/退避。
        from app.services.backoff import parse_retry_after
        from app.services.dashscope_asr_service import DashScopeASRHttpError

        try:
            task_status, output = await self._asr.query_task_once(task_id)
        except DashScopeASRHttpError as exc:
            if exc.status_code == 429:
                raise ProviderError(ERR_RATE_LIMIT, "轮询限流", retryable=True, code="rate_limited",
                                    retry_after_seconds=parse_retry_after(exc.retry_after)) from exc
            if exc.status_code >= 500:
                raise ProviderError(ERR_PROVIDER, f"轮询HTTP {exc.status_code}", retryable=True) from exc
            raise ProviderError(
                ERR_PROVIDER, f"轮询HTTP {exc.status_code}", retryable=False,
                code="invalid_auth" if exc.status_code in (401, 403) else "invalid_request",
            ) from exc
        if task_status == "SUCCEEDED":
            # 轮询不再删源对象：超时/失败不提前删源（CA-08），由终态后 cleanup 统一清理。
            segments = self._asr._parse_result(output)
            if not segments:
                raise ProviderError(ERR_NO_SPEECH, "未识别到语音内容", retryable=False)
            return segments
        if task_status == "FAILED":
            raise ProviderError(ERR_PROVIDER, "DashScope 任务失败", retryable=False, code="task_failed")
        return None  # PENDING / RUNNING：仍在处理，由服务按间隔继续轮询

    async def cleanup(self, object_key: str | None) -> bool:
        if not object_key:
            return True
        return self._oss.delete_object_key(object_key)


# ── provider 选择 ────────────────────────────────────────────────────
_provider_override: Optional[ASRProvider] = None


def set_provider(provider: ASRProvider) -> None:
    global _provider_override
    _provider_override = provider


def clear_provider_override() -> None:
    global _provider_override
    _provider_override = None


def get_provider() -> ASRProvider:
    if _provider_override is not None:
        return _provider_override
    mode = settings.ASR_MODE
    if mode == "fake":
        return FakeASRProvider()
    if mode == "demo":
        return DemoASRProvider()
    return DashScopeASRProvider()
