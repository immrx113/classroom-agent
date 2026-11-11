"""统一调用网关（专项「Checkpoint + AI 调用控制」S3）

所有实际外发供应商请求（ASR submit/query、整理 extract/summarize、索引 embed、
IndexIntent）统一经此网关，顺序为：令牌桶 → 并发 permit → 短事务预算预留 + 调用记录
→ 标记 in_flight → 单次外部调用 → 结果/调用状态/预算结算 → 释放 permit。

- 每次实际出网 attempt 都有令牌结果、独立 provider_calls 记录与适用预算（CA-15）。
- 等待（限流/并发满/Redis 不可用）不占数据库长锁、不占 permit、不增加 attempt/预算（CA-17）。
- 预留不超可用额，attempt_id 幂等结算不重复扣款（CA-18）。
- 非幂等超时/结果未知 → provider_unknown，阻断普通重试（CA-07/CA-20）。
- attempt_no 为该 operation 的实际请求次数（首次含内最多 3 次），重启/普通重试不清零（CA-21）。
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.processing_job import ProcessingJob, ProviderCall
from app.services import call_budget_service as budget
from app.services import lease_service
from app.services import provider_permit as permit
from app.services import rate_limit_service as rl
from app.services.backoff import classify_exception, compute_backoff
from app.services.checkpoint_service import record_provider_call
from app.services.errors import BudgetExhaustedError, CallCancelled, ProviderUnknownError

logger = logging.getLogger(__name__)

# 控制等待原因（落库 wait_reason，不消耗外部 attempt）
WAIT_CONCURRENCY = "concurrency_limit"


def json_serializer(result: Any) -> dict:
    """通用结果序列化：结果须为 JSON 可序列化（dict/list/标量），存 provider_calls.result_ref。"""
    return {"result": result}


def json_deserializer(data: dict) -> Any:
    return data["result"]


def _extract_task_id(result: Any) -> str | None:
    """从成功结果中提取供应商 task_id（提交/轮询结果），持久化供恢复沿用。"""
    if result is None:
        return None
    if hasattr(result, "task_id") and isinstance(result.task_id, str):
        return result.task_id
    if isinstance(result, dict) and isinstance(result.get("task_id"), str):
        return result["task_id"]
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ControlWaitError(Exception):
    """供应商在途 permit 已满：控制等待，不占 permit、不增加外部 attempt/预算（CA-17）。"""

    def __init__(self, wait_seconds: float | None = None):
        super().__init__("provider permit exhausted")
        self.wait_seconds = wait_seconds


class CallSpec:
    """一次受控外部调用的元数据。"""

    def __init__(
        self,
        *,
        operation_key: str,
        operation_type: str,
        operation_family: str,
        provider: str,
        model: str | None = None,
        input_hash: str | None = None,
        job_id=None,
        checkpoint_id=None,
        subject_id: str | None = None,
        is_idempotent: bool = True,
        estimated_cost_micro: int | None = None,
        owner: str | None = None,
        epoch: int = 0,
        provider_task_id: str | None = None,
        deadline: datetime | None = None,
        result_serializer: Callable[[Any], dict] | None = None,
        result_deserializer: Callable[[dict], Any] | None = None,
    ):
        self.operation_key = operation_key
        self.operation_type = operation_type
        self.operation_family = operation_family
        self.provider = provider
        self.model = model
        self.input_hash = input_hash
        self.job_id = job_id
        self.checkpoint_id = checkpoint_id
        self.subject_id = subject_id or settings.LOCAL_SUBJECT_ID
        self.is_idempotent = is_idempotent
        self.estimated_cost_micro = estimated_cost_micro
        self.owner = owner
        self.epoch = epoch
        self.provider_task_id = provider_task_id
        # ASR 轮询等总期限锚定外部（提交时刻），不随每次查询重置（CA-22）。
        self.deadline = deadline
        # 成功结果的持久化引用（关闭成功提交至业务 Checkpoint 间失联窗口，CA-07）。
        self.result_serializer = result_serializer
        self.result_deserializer = result_deserializer


@dataclass
class CallOutcome:
    """单次受控调用结果。"""

    kind: str  # success / retry / permanent / unknown / budget_exhausted / control_wait
    result: Any = None
    error: Exception | None = None
    attempt_no: int = 0
    attempt_id: uuid.UUID | None = None
    retry_after_seconds: int | None = None
    backoff_seconds: float | None = None
    deadline: datetime | None = None
    settled_micro: int = 0
    usage_state: str | None = None
    wait_reason: str | None = None
    error_code: str | None = None


async def _next_attempt_no(db: AsyncSession, operation_key: str) -> int:
    row = (
        await db.execute(
            select(func.max(ProviderCall.attempt_no)).where(
                ProviderCall.operation_key == operation_key
            )
        )
    ).scalar()
    return (row or 0) + 1


async def _first_sent_at(db: AsyncSession, operation_key: str) -> datetime | None:
    row = (
        await db.execute(
            select(func.min(ProviderCall.started_at)).where(
                ProviderCall.operation_key == operation_key,
                ProviderCall.started_at.is_not(None),
            )
        )
    ).scalar()
    return row


async def _operation_deadline(db: AsyncSession, operation_key: str, now: datetime) -> datetime:
    """外部操作重试总期限：从首次实际发出计 900 秒，重启不清零。"""
    first = await _first_sent_at(db, operation_key)
    if first is None:
        first = now
    return first + timedelta(seconds=settings.CHECKPOINT_EXT_OP_TIMEOUT_SECONDS)


async def _settle_amount(kind: str, error: Exception | None, reserved_micro: int) -> tuple[int, str]:
    """结算金额与 usage_state：确认未发送释放；可能已发送/未知保留整份（CA-19）。"""
    if kind == "retry":
        error_type = getattr(error, "error_type", None) or getattr(error, "code", None)
        if error_type in ("rate_limited", "config_error"):
            return 0, "confirmed_not_sent"
        return reserved_micro, "unknown"
    if kind == "unknown":
        return reserved_micro, "unknown"
    if kind == "permanent":
        return reserved_micro, "confirmed_sent"
    return reserved_micro, "confirmed_sent"  # success（usage 未知则保守保留整份）


async def _recover_prior_attempt(
    db: AsyncSession, spec: CallSpec, now_fn: Callable[[], datetime]
) -> CallOutcome | None:
    """恢复上一次失联/成功/终止遗留的调用记录，阻断重复收费（CA-07/CA-21/CA-23）。

    - reserved/in_flight：worker 失联无法确认结果，结算为 unknown 保留预留；
      幂等操作（轮询查询）结算后安全重发，非幂等操作阻断重发。
    - 持久终止决定（deadline_exhausted/attempt_limit_exhausted）：幂等/非幂等一律先执行，
      恢复直接按永久失败返回，不重新外发、不绕过已确定的停止决定与总期限（CA-21）。
    - 非幂等已有 unknown：阻断普通重试，转人工核对，不重新出网/收费。
    - 非幂等已有 succeeded：读取持久化 result_ref 交给业务恢复，不因 Checkpoint 未写重新收费。
    """
    # 1) 结算遗留 reserved/in_flight（失联窗口）
    stale = (
        await db.execute(
            select(ProviderCall).where(
                ProviderCall.operation_key == spec.operation_key,
                ProviderCall.status.in_(("reserved", "in_flight")),
            )
        )
    ).scalars().all()
    for pc in stale:
        kept = pc.reserved_amount or budget.reserve_min_micro()
        await budget.settle_budget(
            db, attempt_id=pc.id, settled_micro=kept, usage_state="unknown"
        )
        pc.status = "unknown"
        pc.finished_at = now_fn()
        pc.settled_amount = kept
        pc.usage_state = "unknown"
        pc.error_code = "provider_unknown"
    if stale:
        await db.commit()

    # 2) 持久终止决定优先于幂等/非幂等分流：已判定超期/次数耗尽的操作，恢复一律不重发（CA-21）。
    terminated = (
        await db.execute(
            select(ProviderCall).where(
                ProviderCall.operation_key == spec.operation_key,
                ProviderCall.status.in_(("deadline_exhausted", "attempt_limit_exhausted")),
            ).order_by(ProviderCall.attempt_no.desc())
        )
    ).scalars().first()
    if terminated is not None:
        return CallOutcome(
            kind="permanent",
            error_code=terminated.status,
            attempt_no=(terminated.attempt_no or 0),
            attempt_id=terminated.id,
            usage_state=terminated.usage_state or "confirmed_not_sent",
            settled_micro=(terminated.settled_amount or 0),
        )

    if spec.is_idempotent:
        return None  # 幂等查询：遗留 unknown 结算后可安全重发（保留 unknown 安全重查规则）

    # 3) 非幂等：取最近一条终态（unknown/succeeded）决定阻断重发或恢复结果
    terminal = (
        await db.execute(
            select(ProviderCall).where(
                ProviderCall.operation_key == spec.operation_key,
                ProviderCall.status.in_(("unknown", "succeeded")),
            ).order_by(ProviderCall.attempt_no.desc())
        )
    ).scalars().first()
    if terminal is None:
        return None
    if terminal.status == "unknown":
        return CallOutcome(
            kind="unknown",
            error_code="provider_unknown",
            attempt_no=(terminal.attempt_no or 0),
            attempt_id=terminal.id,
            usage_state="unknown",
            settled_micro=(terminal.settled_amount or 0),
        )
    # succeeded：恢复持久化结果，不再重新外发/收费
    if spec.result_deserializer is not None and terminal.result_ref is not None:
        result = spec.result_deserializer(terminal.result_ref)
        return CallOutcome(
            kind="success",
            result=result,
            attempt_no=(terminal.attempt_no or 0),
            attempt_id=terminal.id,
            settled_micro=(terminal.settled_amount or 0),
            usage_state=terminal.usage_state,
        )
    # 无结果序列化器（历史记录）：无法恢复结果，但也不得重新收费——转 unknown 核对
    return CallOutcome(
        kind="unknown",
        error_code="provider_unknown",
        attempt_no=(terminal.attempt_no or 0),
        attempt_id=terminal.id,
        usage_state="unknown",
        settled_micro=(terminal.settled_amount or 0),
    )


async def run_operation(
    db: AsyncSession,
    spec: CallSpec,
    transport: Callable[[], Awaitable[Any]],
    *,
    rng=None,
    now_override: Callable[[], datetime] | None = None,
) -> CallOutcome:
    """执行一次受控外部操作（单 attempt，含重试期限与次数上限）。

    ``transport`` 只负责一次真实 HTTP/供应商请求与结果解析（无 SDK 隐藏重试、无预算）。
    返回 CallOutcome；服务按 kind 落库终态/等待/失败。
    """
    now_fn = now_override or _now
    now = now_fn()

    # CA-23：进入即检查取消与租约（先于恢复/退避判定），取消/失去租约后不再发起新请求。
    if spec.job_id is not None:
        from app.models.processing_job import ProcessingJob

        cancelled = (
            await db.execute(
                select(ProcessingJob.cancel_requested).where(ProcessingJob.id == spec.job_id)
            )
        ).scalar()
        if cancelled:
            raise CallCancelled()
        if spec.owner and not await lease_service.assert_owner(db, spec.job_id, spec.owner, spec.epoch):
            raise lease_service.StaleLeaseError()

    recovered = await _recover_prior_attempt(db, spec, now_fn)
    if recovered is not None:
        # 恢复沿用已持久化调用记录，不重新外发（CA-24 复用计数：resume_count 只增不改写）。
        if spec.job_id is not None:
            await db.execute(
                update(ProcessingJob)
                .where(ProcessingJob.id == spec.job_id)
                .values(resume_count=ProcessingJob.resume_count + 1)
            )
        return recovered

    attempt_no = await _next_attempt_no(db, spec.operation_key)
    deadline = spec.deadline or await _operation_deadline(db, spec.operation_key, now)

    if attempt_no > settings.CHECKPOINT_EXT_MAX_ATTEMPTS:
        return CallOutcome(
            kind="permanent", attempt_no=attempt_no, deadline=deadline,
            error_code="attempt_limit_exhausted",
        )
    if now >= deadline:
        return CallOutcome(
            kind="permanent", attempt_no=attempt_no, deadline=deadline,
            error_code="deadline_exhausted",
        )

    # 持久退避（CA-21）：上一 attempt 的 retry_at 未到期时继续等待，不重复出网；
    # 重启/普通重试读取持久等待时间，等待不清零。
    pending = (
        await db.execute(
            select(ProviderCall).where(
                ProviderCall.operation_key == spec.operation_key,
                ProviderCall.retry_at.is_not(None),
            ).order_by(ProviderCall.attempt_no.desc())
        )
    ).scalars().first()
    if pending is not None and pending.retry_at > now:
        wait = max(0.0, (pending.retry_at - now).total_seconds())
        return CallOutcome(
            kind="retry",
            attempt_no=(pending.attempt_no or 0),
            attempt_id=pending.id,
            backoff_seconds=wait,
            retry_after_seconds=max(1, int(wait)),
            deadline=deadline,
            usage_state="confirmed_not_sent",
        )

    # 1. 令牌桶（供应商入口；拒绝/Redis 故障向上抛，由服务落库控制等待，不出网）
    try:
        await rl.consume_provider_bucket(spec.operation_family, spec.provider)
    except (rl.RateLimitedError, rl.RateLimitUnavailableError):
        raise

    # 2. 并发 permit（数据库槽位，最多 2 个在途；满则控制等待）
    #    业务实际外发必须占位：无租约上下文（IndexIntent/直接调用）绑定一次性执行身份，
    #    与有租约任务共用同一固定槽位池，不再走免占位旁路（CA-17）。
    permit_owner = spec.owner or f"call-{uuid.uuid4().hex[:12]}"
    slot = None
    renew_stop: asyncio.Event | None = None
    renew_task: asyncio.Task | None = None
    try:
        slot = await permit.acquire_permit(
            db, job_id=spec.job_id, owner=permit_owner, epoch=spec.epoch,
            operation_key=spec.operation_key,
        )
        if slot is None:
            await db.commit()
            raise ControlWaitError(wait_seconds=settings.CHECKPOINT_PROVIDER_PERMIT_TTL_SECONDS)
        await db.commit()

        # 3. 预算预留 + 调用记录（同事务；预留失败则不出网）
        #    首次预留前把旧 SQLite 试用账本迁入 trial_total（CA-19，独立短事务）。
        await budget.ensure_legacy_trial_migrated(spec.subject_id)
        try:
            pc = await record_provider_call(
                db,
                operation_key=spec.operation_key,
                attempt_no=attempt_no,
                job_id=spec.job_id,
                checkpoint_id=spec.checkpoint_id,
                subject_id=spec.subject_id,
                provider=spec.provider,
                model=spec.model,
                operation_type=spec.operation_type,
                input_hash=spec.input_hash,
                provider_task_id=spec.provider_task_id,
                status="reserved",
                started_at=None,
            )
            # attempt_id 与 provider_calls.id 对齐：预留/结算按同一主键幂等（CA-18/CA-19）。
            attempt_id = pc.id
            reserved_micro = await budget.reserve_budget(
                db,
                attempt_id=attempt_id,
                operation_key=spec.operation_key,
                attempt_no=attempt_no,
                subject_id=spec.subject_id,
                amount_micro=spec.estimated_cost_micro,
            )
        except BudgetExhaustedError:
            raise
        # 标记 in_flight（实际发出），落库 reserved 金额
        pc.status = "in_flight"
        pc.started_at = now_fn()
        pc.reserved_amount = reserved_micro
        await db.commit()

        # 长调用期间独立 session 周期续租 permit（CA-17）：主 session 阻塞时槽位不因过期被回收。
        renew_stop = asyncio.Event()
        renew_task = asyncio.create_task(
            permit.renew_loop(spec.job_id, permit_owner, spec.epoch, renew_stop)
        )

        # 4. 单次外部调用
        try:
            result = await transport()
        except ProviderUnknownError as e:
            return await _finish(db, spec, attempt_id, attempt_no, now_fn,
                                 "unknown", e, reserved_micro, slot, deadline, rng=rng)
        except Exception as e:  # noqa: BLE001
            if hasattr(e, "retryable") or hasattr(e, "error_type"):
                # 已分类供应商错误（ProviderError/ModelError）
                cls = classify_exception(e, is_idempotent=spec.is_idempotent)
                kind = {"retryable": "retry", "permanent": "permanent", "unknown": "unknown"}[cls]
                return await _finish(db, spec, attempt_id, attempt_no, now_fn,
                                     kind, e, reserved_micro, slot, deadline, rng=rng)
            # worker 失联/未分类异常：不结算，保留 in_flight 调用记录与预留，供恢复识别 unknown 窗口
            raise

        # 5. 成功：结算（usage 未知则保守保留整份），释放 permit
        return await _finish(db, spec, attempt_id, attempt_no, now_fn,
                             "success", None, reserved_micro, slot, deadline,
                             result=result, rng=rng)
    finally:
        if renew_task is not None:
            renew_stop.set()
            renew_task.cancel()
            try:
                await renew_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if slot is not None:
            await permit.release_permit(db, job_id=spec.job_id, owner=permit_owner, epoch=spec.epoch)
            await db.commit()


async def _finish(
    db: AsyncSession,
    spec: CallSpec,
    attempt_id: uuid.UUID,
    attempt_no: int,
    now_fn: Callable[[], datetime],
    kind: str,
    error: Exception | None,
    reserved_micro: int,
    slot: int,
    deadline: datetime,
    result: Any = None,
    rng=None,
) -> CallOutcome:
    """结算 + 更新调用状态 + 决定重试/等待/失败。

    结算、终态/失败状态与重试决策（retry_at）在同一事务原子提交（CA-21）：
    不在「先提交失败状态、后写 retry_at」之间留下失联窗口，恢复不会绕过持久等待。
    """
    settled_micro, usage_state = await _settle_amount(kind, error, reserved_micro)
    error_code = (getattr(error, "code", None) or getattr(error, "error_type", None)) if error else None

    now = now_fn()

    # 先计算重试决策（不落库），使 retry_at 与状态/结算在同一次 commit 原子持久化（CA-21）。
    final_kind = kind
    final_error_code = error_code
    backoff_seconds = None
    retry_after_seconds = None
    retry_at = None
    if kind == "retry":
        retry_after = getattr(error, "retry_after_seconds", None)
        backoff = compute_backoff(attempt_no, retry_after=retry_after, rng=rng)
        if now + timedelta(seconds=backoff) >= deadline:
            final_kind = "permanent"
            final_error_code = "deadline_exhausted"
        elif attempt_no >= settings.CHECKPOINT_EXT_MAX_ATTEMPTS:
            final_kind = "permanent"
            final_error_code = "attempt_limit_exhausted"
        else:
            backoff_seconds = backoff
            retry_after_seconds = max(1, int(backoff))
            retry_at = now + timedelta(seconds=backoff)

    # 持久状态（CA-21）：success/unknown 直接对应；重试转为终止（超期/次数耗尽）时用专用状态
    # 持久化终止决定，供恢复识别且不重新外发；error_code 仍保留原供应商错误信息。
    if final_kind == "success":
        status = "succeeded"
    elif final_kind == "unknown":
        status = "unknown"
    elif final_kind == "permanent" and final_error_code in ("deadline_exhausted", "attempt_limit_exhausted"):
        status = final_error_code
    else:
        status = "failed"

    pc = (
        await db.execute(select(ProviderCall).where(ProviderCall.id == attempt_id))
    ).scalars().first()
    if pc is not None:
        pc.status = status
        pc.error_code = error_code
        pc.finished_at = now
        pc.settled_amount = settled_micro
        pc.usage_state = usage_state
        pc.retry_at = retry_at
        if kind == "success" and result is not None:
            # 持久化成功结果引用 + 供应商 task_id（CA-07：Checkpoint 未写前失联可复用结果；
            # CA-21/CA-22：提交后沿用同一 task_id，不因超时重复提交/查询）。
            if spec.result_serializer is not None:
                pc.result_ref = spec.result_serializer(result)
            tid = _extract_task_id(result)
            if tid is not None:
                pc.provider_task_id = tid
    await budget.settle_budget(
        db, attempt_id=attempt_id, settled_micro=settled_micro, usage_state=usage_state
    )
    # 单次原子提交：状态 + 结算 + retry_at（CA-21）。
    await db.commit()

    outcome = CallOutcome(
        kind=final_kind,
        result=result,
        error=error,
        attempt_no=attempt_no,
        attempt_id=attempt_id,
        settled_micro=settled_micro,
        usage_state=usage_state,
        deadline=deadline,
        error_code=final_error_code,
    )
    if final_kind == "retry":
        outcome.backoff_seconds = backoff_seconds
        outcome.retry_after_seconds = retry_after_seconds
    return outcome


async def call_controlled(
    db: AsyncSession,
    spec: CallSpec,
    transport: Callable[[], Awaitable[Any]],
    *,
    rng=None,
    now_override: Callable[[], datetime] | None = None,
) -> CallOutcome:
    """受控执行一个外部操作直至终态（success/permanent/unknown）。

    临时失败按有界退避同步重试（attempt_no 1→max、deadline 900s 内），等待期间不持
    permit（run_operation 返回 retry 前已释放）。控制等待/限流/Redis 故障/预算不足
    向上抛（RateLimitedError/RateLimitUnavailableError/ControlWaitError/
    BudgetExhaustedError），由调用方落库 retry_wait 或终态（CA-17/CA-21）。
    """
    while True:
        outcome = await run_operation(db, spec, transport, rng=rng, now_override=now_override)
        if outcome.kind != "retry":
            return outcome
        await asyncio.sleep(outcome.backoff_seconds or 0.0)


async def operation_deadline(
    db: AsyncSession, operation_key: str, now: datetime | None = None
) -> datetime:
    """某操作的总期限：从该 operation 首次实际发出计 900 秒，重启不清零（CA-21/CA-22）。"""
    now = now or _now()
    first = await _first_sent_at(db, operation_key)
    return (first or now) + timedelta(seconds=settings.CHECKPOINT_EXT_OP_TIMEOUT_SECONDS)
