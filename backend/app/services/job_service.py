"""持久任务状态机、outbox 派发、worker 恢复、重试/取消

任务状态：queued → running → succeeded / failed / cancelled；
retry_wait 表示等待下次尝试。stage 记录处理阶段（created/submitting/submitted/
polling/parsing/validating/publishing），供应商 task_id 持久化后恢复沿用同一任务。

派发与恢复（至少一次投递）：
- 上传事务内写入 job + outbox（pending），提交后事件可重投不丢任务；
- worker 失联（running 且心跳超过 T）重入队；retry_wait 到期重入队；
- 重复投递由任务幂等 + output_version 唯一发布保证不产生重复产物。
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audio_asset import AudioAsset
from app.models.processing_job import ProcessingJob, OutboxEvent
from app.services import lease_service, transcript_service
from app.services import rate_limit_service
from app.services.ai_call_gateway import ControlWaitError
from app.services.asr_provider import ProviderError, SubmitResult, get_provider
from app.services.checkpoint_service import (
    CheckpointCorrupt,
    checkpoint_intact,
    get_checkpoint,
    mark_checkpoint_succeeded,
    norm_json,
    sha256_hex,
    upsert_checkpoint,
)
from app.services.errors import (
    ERR_PROVIDER,
    BudgetExhaustedError,
    CallCancelled,
    ProviderUnknownError,
    ServiceError,
)

logger = logging.getLogger(__name__)


class JobCancelled(Exception):
    pass


class WorkerLost(Exception):
    """模拟 worker 失联（自动化验证）：不改写任务终态，已提交产物由恢复沿用。"""


# 测试注入：asr.submit / asr.result 落库后抛 WorkerLost。生产恒为 None。
_submit_committed_hook = None
_result_committed_hook = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _retry_delay(attempt: int) -> timedelta:
    return timedelta(seconds=settings.JOB_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))


async def _asset_local_path(db: AsyncSession, asset_id: uuid.UUID) -> str:
    asset = await db.get(AudioAsset, asset_id)
    if asset is None or not asset.storage_key:
        raise ServiceError(422, "invalid_input", "音频资产缺失", retryable=False)
    return str(Path(settings.UPLOAD_DIR) / asset.storage_key)


async def _check_cancel(db: AsyncSession, job: ProcessingJob) -> None:
    await db.refresh(job)
    if job.cancel_requested:
        raise JobCancelled()


def _asr_input_hash(job: ProcessingJob) -> str:
    """ASR 固定输入身份：绑定音频资产（audio_asset_id）。"""
    return sha256_hex(f"audio_asset:{job.input_version}")


async def _record_asr_result(
    db: AsyncSession, job: ProcessingJob, segments: list[dict],
    owner: str | None = None, epoch: int = 0,
) -> list[dict]:
    """保存原始成功结果到 Checkpoint（CA-06）：解析/发布失败后复用，不再向供应商取结果。

    提交结果 Checkpoint 前加行锁核对归属（CA-11）：迟到 poll 响应不得作为旧执行者产物提交。
    """
    if owner and not await lease_service.assert_owner(db, job.id, owner, epoch, for_update=True):
        raise lease_service.StaleLeaseError()
    await mark_checkpoint_succeeded(
        db,
        job_id=job.id,
        step_key="asr.result",
        input_hash=_asr_input_hash(job),
        config_hash=job.config_hash,
        output_data={"segments": segments},
        output_hash=sha256_hex(norm_json({"segments": segments})),
    )
    await db.commit()
    if _result_committed_hook is not None:
        _result_committed_hook()
    return segments


def _submit_result_serializer(result: SubmitResult) -> dict:
    """提交结果持久化（CA-07）：成功提交后、Checkpoint 未写前失联可恢复回执。"""
    return {"task_id": result.task_id, "object_key": result.object_key}


def _submit_result_deserializer(data: dict) -> SubmitResult:
    return SubmitResult(task_id=data["task_id"], object_key=data.get("object_key"))


async def _recover_poll_seq(db: AsyncSession, job_id: uuid.UUID) -> int:
    """恢复已持久化的轮询查询序列（CA-22）：按最大 seq 的最新 attempt 状态决定推进或沿用。

    - 最新查询已结束（succeeded=成功 pending/终态、unknown=结果未知已结算可安全重查）：
      推进到下一 seq，成功 pending 换新 key 使连续错误重置，不累加耗尽上限。
    - 最新查询未结束（in_flight 失联）或待重试（failed/终止决定）：
      沿用原 seq 与原 key，继续计同一 attempt_no，受最多 3 次上限约束，期限不重置。
    """
    from app.models.processing_job import ProviderCall

    prefix = f"asr:poll:{job_id}:"
    rows = (
        await db.execute(
            select(ProviderCall).where(ProviderCall.operation_key.like(prefix + "%"))
        )
    ).scalars().all()
    by_seq: dict[int, ProviderCall] = {}
    max_seq = 0
    for row in rows:
        suffix = row.operation_key[len(prefix):] if row.operation_key.startswith(prefix) else row.operation_key.rsplit(":", 1)[-1]
        if not suffix.isdigit():
            continue
        seq = int(suffix)
        max_seq = max(max_seq, seq)
        prev = by_seq.get(seq)
        if prev is None or (row.attempt_no or 0) > (prev.attempt_no or 0):
            by_seq[seq] = row
    if not by_seq:
        return 0
    latest = by_seq[max_seq]
    if latest.status in ("succeeded", "unknown"):
        return max_seq + 1
    return max_seq


async def _gateway_poll_loop(
    db: AsyncSession, job: ProcessingJob, provider, deadline, owner, epoch, rng
) -> list[dict]:
    """网关版 ASR 轮询（CA-22）：每次查询为独立受控操作，pending 按间隔继续，
    终态失败不再无限轮询，总期限锚定提交时刻不随查询重置。

    查询序列持久/恢复（CA-22）：按最大 seq 的最新查询状态恢复（成功 pending 推进、未完成/
    待重试沿用），正常 pending 不因重启耗尽错误尝试；连续失败由 ``run_operation`` 对同一
    key 的 attempt_no 计数封顶（最多 3 次），成功后换 key 重置。
    """
    from app.services.ai_call_gateway import CallSpec, call_controlled

    query_seq = await _recover_poll_seq(db, job.id)
    while True:
        if _now() >= deadline:
            raise ProviderError(ERR_PROVIDER, "asr 轮询期限耗尽", retryable=False,
                                code="poll_deadline_exhausted")
        spec = CallSpec(
            operation_key=f"asr:poll:{job.id}:{query_seq}",
            operation_type="asr_poll",
            operation_family=rate_limit_service.FAMILY_ASR_QUERY,
            provider=provider.name,
            model=settings.TRANSCRIPTION_MODEL,
            job_id=job.id,
            checkpoint_id=job.id,
            subject_id=job.subject_id,
            is_idempotent=True,
            owner=owner,
            epoch=epoch,
            deadline=deadline,
        )
        outcome = await call_controlled(db, spec, lambda: provider.poll(job.provider_task_id), rng=rng)
        if outcome.kind == "success":
            segments = outcome.result
            if segments is None:
                # pending：按正常间隔继续轮询，不重提、不消耗 submit 重试（CA-22）
                await asyncio.sleep(settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS)
                query_seq += 1
                continue
            return await _record_asr_result(db, job, segments, owner, epoch)
        if outcome.kind == "unknown":
            # 幂等查询结果未知：可安全重查，保留保守预留（CA-07）
            query_seq += 1
            continue
        # permanent：终态失败 / attempt 耗尽 / 期限耗尽（CA-22 不无限轮询）
        err = outcome.error
        if err is not None:
            raise err
        raise ProviderError(ERR_PROVIDER, outcome.error_code or "poll_failed", retryable=False)


async def _run_pipeline_gateway(
    db: AsyncSession, job: ProcessingJob, provider,
    owner: str | None = None, epoch: int = 0, rng=None,
) -> list[dict]:
    """网关版提交/轮询（S3）：submit 与每次 poll 均经统一网关受控计费（CA-15）。"""
    from app.services.ai_call_gateway import CallSpec, call_controlled, operation_deadline

    # 已成功结果落地：复用，不再 poll（CA-06）
    result_cp = await get_checkpoint(db, job.id, "asr.result")
    if result_cp is not None and result_cp.status == "succeeded":
        if not checkpoint_intact(result_cp, input_hash=_asr_input_hash(job), config_hash=job.config_hash):
            raise CheckpointCorrupt()
        segments = (result_cp.output_data or {}).get("segments")
        if segments:
            return segments

    if job.provider_task_id:
        # 已提交：沿用同一任务恢复轮询，不重复 submit
        job.stage = "polling"
        await db.commit()
        await _check_cancel(db, job)
        if owner and not await lease_service.assert_owner(db, job.id, owner, epoch):
            raise lease_service.StaleLeaseError()
        deadline = await operation_deadline(db, f"asr:submit:{job.id}")
        return await _gateway_poll_loop(db, job, provider, deadline, owner, epoch, rng)

    job.stage = "submitting"
    await db.commit()
    await _check_cancel(db, job)
    local_path = await _asset_local_path(db, job.input_version)
    if owner and not await lease_service.assert_owner(db, job.id, owner, epoch):
        raise lease_service.StaleLeaseError()

    submit_spec = CallSpec(
        operation_key=f"asr:submit:{job.id}",
        operation_type="asr_submit",
        operation_family=rate_limit_service.FAMILY_ASR_SUBMIT,
        provider=provider.name,
        model=settings.TRANSCRIPTION_MODEL,
        job_id=job.id,
        checkpoint_id=job.id,
        subject_id=job.subject_id,
        is_idempotent=False,
        owner=owner,
        epoch=epoch,
        result_serializer=_submit_result_serializer,
        result_deserializer=_submit_result_deserializer,
    )
    submit_outcome = await call_controlled(db, submit_spec, lambda: provider.submit(local_path), rng=rng)
    if submit_outcome.kind == "permanent":
        if submit_outcome.error is not None:
            raise submit_outcome.error
        raise ProviderError(ERR_PROVIDER, submit_outcome.error_code or "submit_failed", retryable=False)
    if submit_outcome.kind == "unknown":
        raise ProviderUnknownError("asr 提交结果未知，阻断普通重试")
    submit = submit_outcome.result

    # 提交回执 task_id 与 OSS object_key 一并持久化（CA-06/CA-08）
    if owner and not await lease_service.assert_owner(db, job.id, owner, epoch, for_update=True):
        raise lease_service.StaleLeaseError()
    job.provider_task_id = submit.task_id
    job.stage = "submitted"
    await mark_checkpoint_succeeded(
        db,
        job_id=job.id,
        step_key="asr.submit",
        input_hash=_asr_input_hash(job),
        config_hash=job.config_hash,
        output_ref=submit.task_id,
        output_data={"task_id": submit.task_id, "object_key": submit.object_key},
    )
    await db.commit()
    if _submit_committed_hook is not None:
        _submit_committed_hook()

    await _check_cancel(db, job)
    job.stage = "polling"
    await db.commit()
    if owner and not await lease_service.assert_owner(db, job.id, owner, epoch):
        raise lease_service.StaleLeaseError()
    deadline = await operation_deadline(db, f"asr:submit:{job.id}")
    return await _gateway_poll_loop(db, job, provider, deadline, owner, epoch, rng)


async def _poll_until_terminal(
    db: AsyncSession, job: ProcessingJob, provider, task_id: str,
    owner: str | None = None, epoch: int = 0,
) -> list[dict]:
    """非网关路径单次查询轮询循环（CA-22）：真实 poll 单次返回 None 表示 pending，
    按间隔继续，期限耗尽明确失败，不无限轮询；每轮核对取消与租约。"""
    deadline = _now() + timedelta(seconds=settings.CHECKPOINT_EXT_OP_TIMEOUT_SECONDS)
    while True:
        await _check_cancel(db, job)
        if owner and not await lease_service.assert_owner(db, job.id, owner, epoch):
            raise lease_service.StaleLeaseError()
        if _now() >= deadline:
            raise ProviderError(ERR_PROVIDER, "asr 轮询期限耗尽", retryable=False,
                                code="poll_deadline_exhausted")
        await rate_limit_service.consume_provider_bucket(
            rate_limit_service.FAMILY_ASR_QUERY, provider.name
        )
        segments = await provider.poll(task_id)
        if segments is None:
            await asyncio.sleep(settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS)
            continue
        return segments


async def _run_pipeline(
    db: AsyncSession, job: ProcessingJob, provider,
    owner: str | None = None, epoch: int = 0,
) -> list[dict]:
    """提交 / 恢复轮询，返回转写片段；供应商 task_id 与 OSS object_key 持久化。"""
    # 已成功结果落地：复用，不再 poll（CA-06）；复用前核对摘要/schema/输入/配置身份（CA-02）
    result_cp = await get_checkpoint(db, job.id, "asr.result")
    if result_cp is not None and result_cp.status == "succeeded":
        if not checkpoint_intact(
            result_cp, input_hash=_asr_input_hash(job), config_hash=job.config_hash
        ):
            raise CheckpointCorrupt()
        segments = (result_cp.output_data or {}).get("segments")
        if segments:
            return segments

    if job.provider_task_id:
        # 已提交：沿用同一任务恢复轮询，不因轮询超时无条件重复提交
        job.stage = "polling"
        await db.commit()
        await _check_cancel(db, job)
        # 外发轮询前核对归属（CA-11）：过期 epoch 不得发起新调用
        if owner and not await lease_service.assert_owner(db, job.id, owner, epoch):
            raise lease_service.StaleLeaseError()
        segments = await _poll_until_terminal(
            db, job, provider, job.provider_task_id, owner, epoch
        )
        return await _record_asr_result(db, job, segments, owner, epoch)

    job.stage = "submitting"
    await db.commit()
    await _check_cancel(db, job)
    local_path = await _asset_local_path(db, job.input_version)
    # 外发提交前核对归属（CA-11）：过期 epoch 不得发起新调用
    if owner and not await lease_service.assert_owner(db, job.id, owner, epoch):
        raise lease_service.StaleLeaseError()
    await rate_limit_service.consume_provider_bucket(
        rate_limit_service.FAMILY_ASR_SUBMIT, provider.name
    )
    submit = await provider.submit(local_path)
    # 外发响应返回后、提交 submit Checkpoint 前：加行锁重新核对归属（CA-11）
    if owner and not await lease_service.assert_owner(db, job.id, owner, epoch, for_update=True):
        raise lease_service.StaleLeaseError()
    job.provider_task_id = submit.task_id
    job.stage = "submitted"
    # 提交回执 task_id 与 OSS object_key 一并持久化（CA-06/CA-08），不再次 submit
    await mark_checkpoint_succeeded(
        db,
        job_id=job.id,
        step_key="asr.submit",
        input_hash=_asr_input_hash(job),
        config_hash=job.config_hash,
        output_ref=submit.task_id,
        output_data={"task_id": submit.task_id, "object_key": submit.object_key},
    )
    await db.commit()
    if _submit_committed_hook is not None:
        _submit_committed_hook()

    await _check_cancel(db, job)
    job.stage = "polling"
    await db.commit()
    # 外发轮询前核对归属（CA-11）
    if owner and not await lease_service.assert_owner(db, job.id, owner, epoch):
        raise lease_service.StaleLeaseError()
    segments = await _poll_until_terminal(
        db, job, provider, submit.task_id, owner, epoch
    )
    return await _record_asr_result(db, job, segments, owner, epoch)


async def _cleanup_source(db: AsyncSession, job: ProcessingJob, provider) -> bool:
    """终态后清理源对象（CA-08）：幂等、可重试、不重提 ASR。返回是否已完成清理。"""
    submit_cp = await get_checkpoint(db, job.id, "asr.submit")
    object_key = None
    if submit_cp is not None and submit_cp.status == "succeeded":
        object_key = (submit_cp.output_data or {}).get("object_key")
    if not object_key:
        return True
    clean_cp = await get_checkpoint(db, job.id, "asr.cleanup")
    if clean_cp is not None and clean_cp.status == "succeeded":
        return True
    ok = await provider.cleanup(object_key)
    if ok:
        await mark_checkpoint_succeeded(
            db,
            job_id=job.id,
            step_key="asr.cleanup",
            input_hash=_asr_input_hash(job),
            config_hash=job.config_hash,
            output_ref=object_key,
        )
    else:
        await upsert_checkpoint(db, job_id=job.id, step_key="asr.cleanup", status="failed")
    await db.commit()
    return ok


async def _cleanup_if_terminal(db: AsyncSession, job: ProcessingJob, provider) -> None:
    """终态（succeeded/failed/cancelled）后清理源对象；retry_wait 保留源对象供重试。"""
    await db.refresh(job)
    if job.status in ("succeeded", "failed", "cancelled"):
        await _cleanup_source(db, job, provider)


async def retry_source_cleanup(db: AsyncSession, provider=None) -> int:
    """终态 ASR 任务中清理失败者重试（CA-08），不重提 ASR。返回重试次数。"""
    provider = provider or get_provider()
    rows = (
        await db.execute(
            select(ProcessingJob).where(
                ProcessingJob.type == "asr",
                ProcessingJob.status.in_(("succeeded", "failed", "cancelled")),
            )
        )
    ).scalars().all()
    retried = 0
    for job in rows:
        clean_cp = await get_checkpoint(db, job.id, "asr.cleanup")
        if clean_cp is not None and clean_cp.status == "succeeded":
            continue
        submit_cp = await get_checkpoint(db, job.id, "asr.submit")
        if submit_cp is None or submit_cp.status != "succeeded":
            continue
        await _cleanup_source(db, job, provider)
        retried += 1
    return retried


async def _mark_failure(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    error_type: str,
    code: str,
    retryable: bool,
    owner: str | None = None,
    epoch: int = 0,
) -> bool:
    """执行者触发的失败/等待/取消终态写入，携带 owner/epoch 受租约保护（CA-11）。

    迟到的 ProviderError/CheckpointCorrupt 等异常在写入状态前重新加锁核对归属；
    失去租约时不覆盖新 owner 状态，返回 False（事务已回滚）。返回 True 表示已写入。
    只接收 ``job_id`` 主键，不读取可能已过期（expired）的 ``job`` 对象属性（CA-19：
    预算不足回滚后对象属性过期，惰性加载会触发 MissingGreenlet）。
    """

    def mutate(row: ProcessingJob) -> None:
        row.error_type = error_type
        row.error_code = code or error_type
        if row.cancel_requested:
            row.status = "cancelled"
        elif retryable and (row.attempt or 0) < settings.JOB_MAX_ATTEMPTS:
            row.status = "retry_wait"
            row.retry_at = _now() + _retry_delay(row.attempt or 1)
        else:
            row.status = "failed"

    if owner:
        return await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, mutate)
    row = await db.get(ProcessingJob, job_id)
    if row is None:
        return False
    mutate(row)
    await db.commit()
    return True


async def _reload_job(db: AsyncSession, job_id: uuid.UUID) -> ProcessingJob | None:
    """回滚后重新读取任务（失去租约/未写入时返回当前最新状态，不改写）。"""
    await db.rollback()
    job = await db.get(ProcessingJob, job_id)
    if job is not None:
        await db.refresh(job)
    return job


async def run_job(db: AsyncSession, job_id: uuid.UUID, provider=None) -> ProcessingJob | None:
    """执行单个任务（幂等：终态直接返回，output_version 防止重复发布）。"""
    job = await db.get(ProcessingJob, job_id)
    if job is None:
        return None
    # 回退只读（CA-25）：即使被直接调用也不领取租约、不外发、不发布，任务保持原状态。
    from app.services.access import is_read_only

    if is_read_only():
        await db.refresh(job)
        return job
    if job.status in ("succeeded", "cancelled"):
        return job
    if job.cancel_requested:
        job.status = "cancelled"
        job.error_code = "cancelled"
        await db.commit()
        return job

    # 租约原子领取（CA-11）：实际 ASR 执行入口统一领取，他人有效持有则本次不处理。
    owner = lease_service.new_owner()
    claimed, epoch = await lease_service.claim_job(db, job_id, owner)
    if not claimed:
        await db.commit()
        await db.refresh(job)
        return job
    await db.commit()
    await db.refresh(job)

    # 长调用期间独立 session 周期续租（CA-11）：poll 期间租约不因主 session 阻塞而过期。
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        lease_service.heartbeat_loop(job_id, owner, epoch, heartbeat_stop)
    )

    provider = provider or get_provider()
    try:
        if settings.CALL_GATEWAY_ENABLED:
            segments = await _run_pipeline_gateway(db, job, provider, owner, epoch)
        else:
            segments = await _run_pipeline(db, job, provider, owner, epoch)
        await _check_cancel(db, job)

        # 提交产物前校验租约仍归本执行者（CA-11）：加行锁使校验与发布同事务原子
        if not await lease_service.assert_owner(db, job_id, owner, epoch, for_update=True):
            raise lease_service.StaleLeaseError()

        if job.output_version is None:
            version, is_active = await transcript_service.create_version(
                db,
                lecture_id=job.resource_id,
                audio_asset_id=job.input_version,
                provider=provider.name,
                provider_task_id=job.provider_task_id,
                segments=segments,
            )
            job.output_version = version.id
            # 转写版本、片段、Checkpoint、output_version 同事务提交（CA-06）
            await mark_checkpoint_succeeded(
                db,
                job_id=job.id,
                step_key="transcript.publish",
                input_hash=_asr_input_hash(job),
                config_hash=job.config_hash,
                output_ref=str(version.id),
                output_hash=sha256_hex(norm_json({"version": str(version.id)})),
            )

        job.status = "succeeded"
        job.stage = "publishing"
        job.last_successful_stage = "transcript.publish"
        job.error_code = None
        job.error_type = None
        await db.commit()
        await _cleanup_if_terminal(db, job, provider)
        return job
    except JobCancelled:
        # 执行者响应取消也受租约保护（CA-11）：迟到取消不得改写新 owner 状态
        def _cancel(row: ProcessingJob) -> None:
            row.status = "cancelled"
            row.error_code = "cancelled"

        if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _cancel):
            return await _reload_job(db, job_id)
        await _cleanup_if_terminal(db, job, provider)
        return job
    except CallCancelled:
        # 网关在再次外发前检测到取消（CA-23）：与 JobCancelled 同样标记 cancelled。
        def _cancel(row: ProcessingJob) -> None:
            row.status = "cancelled"
            row.error_code = "cancelled"

        if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _cancel):
            return await _reload_job(db, job_id)
        await _cleanup_if_terminal(db, job, provider)
        return job
    except WorkerLost:
        # 模拟 worker 失联：不改写任务终态，已提交的 submit/result 产物由恢复沿用
        raise
    except lease_service.StaleLeaseError:
        # 租约已被接管/过期：不提交产物、不改写任务状态，交由新 owner 处理（CA-11）
        return await _reload_job(db, job_id)
    except rate_limit_service.RateLimitedError as e:
        # 供应商桶拒绝（CA-16）：落库 retry_wait 并释放租约，不消耗外部 attempt_no、不出网。
        if await rate_limit_service.mark_control_wait(
            db, job_id, owner, epoch, "rate_limited", e.retry_after_seconds
        ):
            return await _reload_job(db, job_id)
        return await _reload_job(db, job_id)
    except rate_limit_service.RateLimitUnavailableError:
        # Redis 不可用（CA-16）：worker 持久等待 ≥5s 再检查，禁止本地无限放行、禁止出网。
        if await rate_limit_service.mark_control_wait(
            db, job_id, owner, epoch, "rate_limit_unavailable", settings.CHECKPOINT_REDIS_WAIT_SECONDS
        ):
            return await _reload_job(db, job_id)
        return await _reload_job(db, job_id)
    except ControlWaitError as e:
        # 供应商在途 permit 已满（CA-17）：落库控制等待，不占 permit/预算/外部 attempt。
        if await rate_limit_service.mark_control_wait(
            db, job_id, owner, epoch, "concurrency_limit",
            e.wait_seconds or settings.CHECKPOINT_PROVIDER_PERMIT_TTL_SECONDS
        ):
            return await _reload_job(db, job_id)
        return await _reload_job(db, job_id)
    except BudgetExhaustedError:
        # 持久预算不足（CA-18/CA-19）：保留 Checkpoint，明确失败不重试、不出网。
        if not await _mark_failure(db, job_id, error_type="budget_exhausted", code="budget_exhausted",
                                   retryable=False, owner=owner, epoch=epoch):
            return await _reload_job(db, job_id)
        await _cleanup_if_terminal(db, job, provider)
        return job
    except ProviderUnknownError:
        # 非幂等外部结果未知（CA-07）：进入 provider_unknown，阻断普通重试，保留未知预留。
        if not await _mark_failure(db, job_id, error_type="provider_unknown", code="provider_unknown",
                                   retryable=False, owner=owner, epoch=epoch):
            return await _reload_job(db, job_id)
        await _cleanup_if_terminal(db, job, provider)
        return job
    except CheckpointCorrupt:
        # 已成功 Checkpoint 引用/摘要/schema 失效：明确失败不重试、不伪成功（CA-02）
        if not await _mark_failure(db, job_id, error_type="checkpoint_corrupt", code="checkpoint_corrupt", retryable=False, owner=owner, epoch=epoch):
            return await _reload_job(db, job_id)
        await _cleanup_if_terminal(db, job, provider)
        return job
    except ProviderError as e:
        if not await _mark_failure(db, job_id, error_type=e.error_type, code=e.code, retryable=e.retryable, owner=owner, epoch=epoch):
            return await _reload_job(db, job_id)
        await _cleanup_if_terminal(db, job, provider)
        return job
    except ServiceError as e:
        if not await _mark_failure(db, job_id, error_type=e.code, code=e.code, retryable=e.retryable, owner=owner, epoch=epoch):
            return await _reload_job(db, job_id)
        await _cleanup_if_terminal(db, job, provider)
        return job
    except Exception as e:  # 兜底：未知异常视为暂时可重试，遵守有界重试
        logger.exception("job %s 处理异常", job.id)
        if not await _mark_failure(db, job_id, error_type=ERR_PROVIDER, code="unknown", retryable=True, owner=owner, epoch=epoch):
            return await _reload_job(db, job_id)
        await _cleanup_if_terminal(db, job, provider)
        return job
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


async def recover_and_dispatch(db: AsyncSession, provider=None) -> dict:
    """恢复失联 worker、重入队到期重试、派发 queued 任务。返回统计。"""
    stats = {"recovered": 0, "dispatched": 0, "succeeded": 0, "failed": 0,
             "retry_wait": 0, "cancelled": 0}

    # 回退只读（CA-25）：停写期不领取、不重入队、不派发任何既有任务，保留原状态与账本。
    from app.services.access import is_read_only

    if is_read_only():
        stats["read_only"] = True
        return stats

    # 1. 失联 worker：running 且心跳超过 T → 重入队
    stale_cutoff = _now() - timedelta(seconds=settings.JOB_HEARTBEAT_TIMEOUT_SECONDS)
    stale = (
        await db.execute(
            select(ProcessingJob).where(
                ProcessingJob.type == "asr",
                ProcessingJob.status == "running",
                ProcessingJob.heartbeat_at.is_not(None),
                ProcessingJob.heartbeat_at < stale_cutoff,
            )
        )
    ).scalars().all()
    for job in stale:
        job.status = "queued"
        stats["recovered"] += 1

    # 2. retry_wait 到期 → queued
    due = (
        await db.execute(
            select(ProcessingJob).where(
                ProcessingJob.type == "asr",
                ProcessingJob.status == "retry_wait",
                ProcessingJob.retry_at.is_not(None),
                ProcessingJob.retry_at <= _now(),
            )
        )
    ).scalars().all()
    for job in due:
        job.status = "queued"
        stats["recovered"] += 1

    if stale or due:
        await db.commit()

    # 3. 派发 queued 任务
    queued = (
        await db.execute(
            select(ProcessingJob).where(
                ProcessingJob.type == "asr", ProcessingJob.status == "queued"
            ).order_by(ProcessingJob.created_at.asc())
        )
    ).scalars().all()
    for job in queued:
        outbox = (
            await db.execute(
                select(OutboxEvent).where(OutboxEvent.job_id == job.id)
            )
        ).scalars().first()
        if outbox is not None and outbox.status == "pending":
            outbox.status = "dispatched"
            outbox.dispatched_at = _now()
            outbox.attempt = (outbox.attempt or 0) + 1
            await db.commit()
        stats["dispatched"] += 1

        await run_job(db, job.id, provider=provider)

        if outbox is not None:
            current = await db.get(ProcessingJob, job.id)
            if current is not None and current.status in ("succeeded", "failed", "cancelled"):
                outbox.status = "consumed"
                outbox.consumed_at = _now()
                await db.commit()
        final = await db.get(ProcessingJob, job.id)
        if final is not None:
            key = final.status if final.status in stats else "failed"
            stats[key] += 1

    # 4. 终态后清理重试（CA-08）：清理失败不重提 ASR
    stats["cleanup_retried"] = await retry_source_cleanup(db, provider)
    return stats


async def retry_job(db: AsyncSession, job_id: uuid.UUID) -> ProcessingJob:
    """显式重试：仅限失败/等待重试/取消终态；绑定原输入，不重复已有产物。"""
    # 回退只读（CA-25）：服务层同样阻断重试重新排队，不依赖路由单独守卫。
    from app.services.access import enforce_writable

    enforce_writable()
    job = await db.get(ProcessingJob, job_id)
    if job is None:
        raise ServiceError(404, "not_found", "任务不存在", retryable=False)
    if job.status not in ("failed", "retry_wait", "cancelled"):
        raise ServiceError(409, "invalid_state", "任务当前状态不可重试", retryable=False)
    if job.error_type == "provider_unknown":
        # 非幂等外部结果未知：需人工核对，阻断普通重试以免重复外发/收费（CA-07）。
        raise ServiceError(409, "provider_unknown", "外部结果未知，需人工核对后重试", retryable=False)

    job.status = "queued"
    job.error_code = None
    job.error_type = None
    job.cancel_requested = False
    job.attempt = 0
    # 保留 provider_task_id / output_version：已提交任务恢复轮询，已发布版本不重复发布
    await db.commit()

    outbox = (
        await db.execute(select(OutboxEvent).where(OutboxEvent.job_id == job.id))
    ).scalars().first()
    if outbox is None:
        db.add(
            OutboxEvent(
                business_key=f"job:{job.id}",
                event_type="asr_requested",
                job_id=job.id,
                payload={"job_id": str(job.id), "resource_id": str(job.resource_id)},
                status="pending",
                attempt=0,
            )
        )
    else:
        outbox.status = "pending"
        outbox.dispatched_at = None
        outbox.consumed_at = None
    await db.commit()
    # updated_at 为 onupdate=now() 的 SQL 表达式，commit 后被置为过期；
    # 同步 _to_response 读取会触发惰性加载（MissingGreenlet），这里先刷回完整属性。
    await db.refresh(job)
    return job


async def cancel_job(db: AsyncSession, job_id: uuid.UUID) -> ProcessingJob:
    """显式取消：queued/retry_wait 直接取消；running 在阶段边界推进；幂等。"""
    job = await db.get(ProcessingJob, job_id)
    if job is None:
        raise ServiceError(404, "not_found", "任务不存在", retryable=False)
    if job.status in ("succeeded", "cancelled"):
        return job
    job.cancel_requested = True
    if job.status in ("queued", "retry_wait"):
        job.status = "cancelled"
        job.error_code = "cancelled"
    await db.commit()
    # 同 retry_job：commit 后 updated_at 过期，先刷回避免同步 _to_response 触发惰性加载
    await db.refresh(job)
    return job


async def get_job_accessible(db: AsyncSession, scope, job_id: uuid.UUID) -> ProcessingJob:
    """查询任务，校验资源归属/归档/范围；越权 404 不泄露存在性。"""
    from app.services.access import get_accessible_lecture

    job = await db.get(ProcessingJob, job_id)
    if job is None:
        raise ServiceError(404, "not_found", "任务不存在", retryable=False)
    await get_accessible_lecture(db, scope, job.resource_id)
    return job


async def get_latest_asr_job(db: AsyncSession, lecture_id: uuid.UUID) -> ProcessingJob | None:
    """课时最新的 ASR 任务（含进行中/失败/终态），供页面刷新后恢复失败原因与重试。"""
    return (
        await db.execute(
            select(ProcessingJob).where(
                ProcessingJob.type == "asr",
                ProcessingJob.resource_id == lecture_id,
            ).order_by(ProcessingJob.created_at.desc()).limit(1)
        )
    ).scalars().first()


def asr_job_to_response(job: ProcessingJob) -> dict:
    """ASR 任务响应（对齐 JobResponse），供课时页恢复最新任务状态。"""
    retryable = job.error_type in ("rate_limited", "timeout", "provider_error")

    def _status_display(j) -> str:
        if j.error_type == "provider_unknown" or j.wait_reason == "provider_unknown":
            return "调用结果待核对"
        if j.error_type == "budget_exhausted":
            return "预算不足"
        return j.status

    return {
        "id": job.id,
        "type": job.type,
        "resource_id": job.resource_id,
        "input_version": job.input_version,
        "status": job.status,
        "stage": job.stage,
        "attempt": job.attempt or 0,
        "cancel_requested": job.cancel_requested,
        "error_code": job.error_code,
        "error_type": job.error_type,
        "retryable": retryable,
        "provider_task_id": job.provider_task_id,
        "output_version": job.output_version,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "wait_reason": job.wait_reason,
        "retry_at": job.retry_at,
        "last_successful_stage": job.last_successful_stage,
        "completed_units": (1 if job.output_version is not None else 0),
        "total_units": 1,
        "resume_count": job.resume_count or 0,
        "status_display": _status_display(job),
    }
