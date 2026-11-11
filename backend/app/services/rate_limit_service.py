"""Redis 令牌桶限流（专项「Checkpoint + AI 调用控制」S2）

- 单实例 Redis + 原子 Lua 桶（``app/scripts/token_bucket.lua``），EVALSHA 命中缓存，
  NOSCRIPT 时安全重载脚本一次；超时不盲重放扣减、不对结果未知的扣减退款。
- 键格式：``rl:v1:<environment>:<policy_version>:<scope_hash>:<operation_family>``，
  Hash 只存 tokens / last_ms，不写 API key / 正文。
- 用户入口按 ``ScopeContext.subject_id × operation_family`` 分桶；供应商入口按
  ``provider（credential_alias，不含密钥）× operation_family`` 分桶，模型名映射到
  供应商实际共享配额组，不能靠换模型名拆分绕过（CA-13）。
- 故障策略（CA-16）：Redis 不可用/脚本缓存故障抛 ``RateLimitUnavailableError``，
  禁止本地无限放行；``RATE_LIMIT_ENABLED=False`` 时完全旁路（不建 Redis 连接），
  保证 S1 及既有阶段验证不依赖 Redis。
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from redis.exceptions import NoScriptError, RedisError, TimeoutError as RedisTimeout

from app.config import settings
from app.services.checkpoint_service import policy_version

logger = logging.getLogger(__name__)

RATE_ENV = "local"  # 隔离命名空间：个人版固定本地环境（多环境不串桶）
KEY_PREFIX = "rl:v1"

_LUA_PATH = Path(__file__).resolve().parents[1] / "scripts" / "token_bucket.lua"

# operation_family 常量
FAMILY_AUDIO_UPLOAD = "audio_upload"
FAMILY_ORGANIZE = "organize"
FAMILY_DOCUMENT_INDEX = "document_index"
FAMILY_EXPLICIT_INDEX = "explicit_index"
FAMILY_RETRY = "retry"
FAMILY_ASR_SUBMIT = "asr_submit"
FAMILY_ASR_QUERY = "asr_query"
FAMILY_GEN = "gen"        # 供应商生成/整理共享组（extract + summarize 共用）
FAMILY_EMBED = "embed"    # embedding 共享组

_POOL: Optional["redis.Redis"] = None
_SCRIPT: Optional[str] = None
_SCRIPT_SHA: Optional[str] = None
_LOCK = asyncio.Lock()


@dataclass
class TokenResult:
    allowed: bool
    remaining: float
    retry_after_ms: int

    @property
    def retry_after_seconds(self) -> int:
        """Retry-After 向上取整且至少 1 秒（5.3）。"""
        return max(1, (self.retry_after_ms + 999) // 1000)


class RateLimitedError(Exception):
    """供应商桶拒绝（worker 路径）：落库 retry_wait + wait_reason=rate_limited。"""

    def __init__(self, retry_after_seconds: int):
        super().__init__(f"rate limited, retry_after={retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class RateLimitUnavailableError(Exception):
    """Redis 不可用 / 脚本缓存故障：禁止本地放行，worker 等待 ≥5s 再检查。"""


def _validate(capacity: float, refill: float, cost: float) -> None:
    if capacity <= 0 or refill <= 0 or cost <= 0 or cost > capacity:
        raise ValueError(f"非法令牌桶配置 capacity={capacity} refill={refill} cost={cost}")


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def user_key(subject_id: str, operation_family: str) -> str:
    return f"{KEY_PREFIX}:{RATE_ENV}:{policy_version()}:{_scope_hash('subject:' + subject_id)}:{operation_family}"


def provider_key(provider: str, operation_family: str) -> str:
    return f"{KEY_PREFIX}:{RATE_ENV}:{policy_version()}:{_scope_hash('provider:' + provider)}:{operation_family}"


def _provider_config(operation_family: str) -> tuple[float, float]:
    if operation_family == FAMILY_ASR_SUBMIT:
        return settings.RATE_ASR_SUBMIT_CAPACITY, settings.RATE_ASR_SUBMIT_REFILL
    if operation_family == FAMILY_ASR_QUERY:
        return settings.RATE_ASR_QUERY_CAPACITY, settings.RATE_ASR_QUERY_REFILL
    if operation_family == FAMILY_GEN:
        return settings.RATE_PROVIDER_GEN_CAPACITY, settings.RATE_PROVIDER_GEN_REFILL
    if operation_family == FAMILY_EMBED:
        return settings.RATE_EMBED_CAPACITY, settings.RATE_EMBED_REFILL
    raise ValueError(f"未知供应商 operation_family: {operation_family}")


async def init_pool() -> None:
    """初始化异步连接池（FastAPI lifespan / worker 启动时调用，退出时 close_pool）。"""
    global _POOL
    if _POOL is None:
        import redis.asyncio as redis

        _POOL = redis.Redis.from_url(
            settings.REDIS_URL,
            max_connections=settings.CHECKPOINT_REDIS_POOL_MAX,
            socket_timeout=settings.CHECKPOINT_REDIS_TIMEOUT_SECONDS,
            socket_connect_timeout=settings.CHECKPOINT_REDIS_TIMEOUT_SECONDS,
        )


async def close_pool() -> None:
    """关闭连接池；脚本缓存 SHA 随之失效，重建后重新加载。"""
    global _POOL, _SCRIPT, _SCRIPT_SHA
    if _POOL is not None:
        try:
            await _POOL.aclose()
        finally:
            _POOL = None
            _SCRIPT = None
            _SCRIPT_SHA = None


async def _get_pool():
    if _POOL is None:
        await init_pool()
    return _POOL


async def _ensure_script(pool) -> str:
    global _SCRIPT, _SCRIPT_SHA
    if _SCRIPT_SHA is None:
        async with _LOCK:
            if _SCRIPT_SHA is None:
                _SCRIPT = _LUA_PATH.read_text(encoding="utf-8")
                _SCRIPT_SHA = await pool.script_load(_SCRIPT)
    return _SCRIPT_SHA


async def _reload_script(pool) -> str:
    """NOSCRIPT / 脚本缓存丢失时重载一次（CA-16）。"""
    global _SCRIPT, _SCRIPT_SHA
    async with _LOCK:
        if _SCRIPT is None:
            _SCRIPT = _LUA_PATH.read_text(encoding="utf-8")
        _SCRIPT_SHA = await pool.script_load(_SCRIPT)
    return _SCRIPT_SHA


def _num(v) -> float:
    if isinstance(v, bytes):
        v = v.decode("utf-8")
    return float(v)


async def consume(
    key: str, capacity: float, refill: float, cost: float = 1.0
) -> TokenResult:
    """原子消费一个令牌桶。Redis 不可用/脚本缓存故障抛 ``RateLimitUnavailableError``。"""
    _validate(capacity, refill, cost)
    pool = await _get_pool()
    try:
        sha = await _ensure_script(pool)
        try:
            result = await pool.evalsha(sha, 1, key, capacity, refill, cost)
        except NoScriptError:
            # Redis 重启 / SCRIPT FLUSH 后脚本缓存丢失：安全重载一次（CA-16）。
            sha = await _reload_script(pool)
            result = await pool.evalsha(sha, 1, key, capacity, refill, cost)
    except RateLimitUnavailableError:
        raise
    except (RedisTimeout, RedisError, OSError) as exc:
        logger.warning("rate limit Redis unavailable: %s", exc)
        raise RateLimitUnavailableError("rate limit service unavailable") from exc
    return TokenResult(
        allowed=int(_num(result[0])) == 1,
        remaining=_num(result[1]),
        retry_after_ms=int(_num(result[2])),
    )


async def enforce_user_bucket(scope, operation_family: str) -> None:
    """用户入口限流：拒绝抛 ``ServiceError(429/503)``（CA-14）。"""
    if not settings.RATE_LIMIT_ENABLED:
        return
    from app.services.errors import ServiceError

    key = user_key(scope.subject_id, operation_family)
    try:
        result = await consume(key, settings.RATE_USER_CAPACITY, settings.RATE_USER_REFILL, 1)
    except RateLimitUnavailableError:
        raise ServiceError(
            503, "rate_limit_unavailable", "限流服务暂不可用，请稍后重试", retryable=True
        ) from None
    if not result.allowed:
        raise ServiceError(
            429, "rate_limited", "请求过于频繁，请稍后重试",
            retryable=True, retry_after_seconds=result.retry_after_seconds,
        )


async def consume_provider_bucket(operation_family: str, provider: str) -> None:
    """worker 外发前检查供应商桶：拒绝抛 ``RateLimitedError``，
    Redis 故障抛 ``RateLimitUnavailableError``（CA-16）。"""
    if not settings.RATE_LIMIT_ENABLED:
        return
    capacity, refill = _provider_config(operation_family)
    result = await consume(provider_key(provider, operation_family), capacity, refill, 1)
    if not result.allowed:
        raise RateLimitedError(result.retry_after_seconds)


async def mark_control_wait(
    db, job_id, owner: str, epoch: int, wait_reason: str, delay_seconds: float
) -> bool:
    """worker 桶拒绝 / Redis 不可用：落库 retry_wait + wait_reason + retry_at，
    释放执行租约（不消耗外部 attempt_no、不预留预算），供恢复时重新领取（5.3）。

    累计控制等待（CA-17）：等待秒数累加，达到 CHECKPOINT_CONTROL_WAIT_SECONDS 后
    明确失败（control_wait_exhausted），不无限等待；每次等待仍不占 permit/预算。

    受租约保护（CA-11）：迟到响应不得改写新 owner 状态。返回 False 表示已失去租约。
    """
    from datetime import datetime, timedelta, timezone

    from app.services import lease_service

    now = datetime.now(timezone.utc)
    delay_seconds = max(0, int(round(delay_seconds)))

    def mutate(row) -> None:
        elapsed = (row.control_wait_elapsed_seconds or 0) + delay_seconds
        row.control_wait_elapsed_seconds = elapsed
        row.lease_owner = None
        row.lease_expires_at = None
        if elapsed >= settings.CHECKPOINT_CONTROL_WAIT_SECONDS:
            row.status = "failed"
            row.wait_reason = wait_reason
            row.error_type = "control_wait_exhausted"
            row.error_code = "control_wait_exhausted"
            row.retry_at = None
        else:
            row.status = "retry_wait"
            row.wait_reason = wait_reason
            row.retry_at = now + timedelta(seconds=delay_seconds)

    return await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, mutate)
