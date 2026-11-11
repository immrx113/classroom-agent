"""阶段级 Checkpoint 与任务身份服务（专项「Checkpoint + AI 调用控制」S1）

- 任务身份：业务键 = subject_id + resource_id + job_type + input_snapshot_hash
  + pipeline_version + semantic_config_hash；策略版本与语义配置分离。
- Checkpoint：``job_checkpoints`` 持久记录每个阶段/单元的已成功产物引用与内容摘要，
  恢复时复用而非重复调用外部服务；``succeeded`` 但引用/摘要/schema 失效返回
  ``checkpoint_corrupt``，不静默重建也不伪成功。
- 调用记录：每次实际外发供应商操作写入 ``provider_calls``，S1 用于调用计数，
  S3 接入统一网关与预算。
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.processing_job import JobCheckpoint, ProcessingJob, ProviderCall

# 管线版本：任务身份的一部分，变更即视为新任务（旧任务仍固定旧身份）
PIPELINE_VERSION = "classroom-v1"
# Checkpoint 输出 schema 版本（结果结构变化即失效）
CHECKPOINT_SCHEMA_VERSION = "1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def norm_json(obj) -> str:
    """规范化 JSON 用于摘要：键排序、紧凑分隔符、无 BOM。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def compute_semantic_hash(obj) -> str:
    return sha256_hex(norm_json(obj))[:32]


def policy_version() -> str:
    """策略版本只含重试/限流参数，不参与语义产物身份（CA-01/CA-02）。"""
    payload = (
        f"{settings.JOB_MAX_ATTEMPTS}|{settings.JOB_RETRY_BASE_SECONDS}|"
        f"{settings.ORGANIZE_MAX_ATTEMPTS}|{settings.INDEX_MAX_ATTEMPTS}|"
        f"{settings.CHECKPOINT_EXT_MAX_ATTEMPTS}|{settings.CHECKPOINT_EXT_BACKOFF_BASE}|"
        f"{settings.CHECKPOINT_EXT_BACKOFF_CAP}|{settings.CHECKPOINT_EXT_OP_TIMEOUT_SECONDS}"
    )
    return sha256_hex(payload)[:16]


def compute_idempotency_key(
    *,
    subject_id: str,
    resource_id,
    job_type: str,
    input_snapshot_hash: str,
    semantic_config_hash: str,
) -> str:
    """任务业务键（规范化 JSON 的 SHA-256 摘要）。"""
    payload = norm_json(
        {
            "subject_id": subject_id,
            "resource_id": str(resource_id),
            "job_type": job_type,
            "input_snapshot_hash": input_snapshot_hash,
            "pipeline_version": PIPELINE_VERSION,
            "semantic_config_hash": semantic_config_hash,
        }
    )
    return sha256_hex(payload)


# ── Checkpoint 读写 ──────────────────────────────────────────────────────
async def get_checkpoint(
    db: AsyncSession, job_id, step_key: str, unit_key: str = "stage"
) -> JobCheckpoint | None:
    row = (
        await db.execute(
            select(JobCheckpoint).where(
                JobCheckpoint.job_id == job_id,
                JobCheckpoint.step_key == step_key,
                JobCheckpoint.unit_key == unit_key,
            )
        )
    ).scalars().first()
    return row


async def upsert_checkpoint(
    db: AsyncSession,
    *,
    job_id,
    step_key: str,
    unit_key: str = "stage",
    input_hash: str | None = None,
    config_hash: str | None = None,
    status: str = "pending",
    output_ref: str | None = None,
    output_data: dict | None = None,
    output_hash: str | None = None,
    schema_version: str | None = CHECKPOINT_SCHEMA_VERSION,
) -> JobCheckpoint:
    cp = await get_checkpoint(db, job_id, step_key, unit_key)
    if cp is None:
        cp = JobCheckpoint(
            job_id=job_id,
            step_key=step_key,
            unit_key=unit_key,
            input_hash=input_hash,
            config_hash=config_hash,
            status=status,
            output_ref=output_ref,
            output_data=output_data,
            output_hash=output_hash,
            schema_version=schema_version,
        )
        db.add(cp)
    else:
        cp.input_hash = input_hash
        cp.config_hash = config_hash
        cp.status = status
        cp.output_ref = output_ref
        cp.output_data = output_data
        cp.output_hash = output_hash
        cp.schema_version = schema_version
    await db.flush()
    return cp


async def mark_checkpoint_succeeded(
    db: AsyncSession,
    *,
    job_id,
    step_key: str,
    unit_key: str = "stage",
    input_hash: str | None = None,
    config_hash: str | None = None,
    output_ref: str | None = None,
    output_data: dict | None = None,
    output_hash: str | None = None,
    schema_version: str | None = CHECKPOINT_SCHEMA_VERSION,
) -> JobCheckpoint:
    cp = await upsert_checkpoint(
        db,
        job_id=job_id,
        step_key=step_key,
        unit_key=unit_key,
        input_hash=input_hash,
        config_hash=config_hash,
        status="succeeded",
        output_ref=output_ref,
        output_data=output_data,
        output_hash=output_hash,
        schema_version=schema_version,
    )
    cp.finished_at = _now()
    await db.flush()
    return cp


async def mark_checkpoint_reused(db: AsyncSession, job_id) -> None:
    """记录一次 Checkpoint 复用：命中已成功产物、跳过外部调用（CA-24）。

    ``reuse_count`` 只增不改写，与 ``resume_count``（网关恢复）共同构成「实际复用」，
    区别于 ``job_checkpoints`` 的「成功产物存量」。
    """
    await db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job_id)
        .values(reuse_count=ProcessingJob.reuse_count + 1)
    )


# 校验结论
CHECKPOINT_OK = "ok"
CHECKPOINT_MISSING = "missing"
CHECKPOINT_CORRUPT = "checkpoint_corrupt"


class CheckpointCorrupt(Exception):
    """已成功 Checkpoint 引用 / 摘要 / schema / 输入 / 配置身份失效：停止并等待人工决定。"""


def checkpoint_intact(
    cp: JobCheckpoint | None,
    *,
    input_hash: str | None = None,
    config_hash: str | None = None,
) -> bool:
    """已成功 Checkpoint 是否仍一致可复用（CA-02）。

    核对 status / schema 版本 / 输入身份 / 配置身份；当 ``output_data`` 存在且
    ``output_hash`` 正是 ``norm_json(output_data)`` 的摘要时，一并核对摘要。
    引用型产物（仅 ``output_ref``、无 ``output_data``）由调用方经 ``validate_checkpoint`` 复核。
    """
    if cp is None or cp.status != "succeeded":
        return False
    if cp.schema_version is not None and cp.schema_version != CHECKPOINT_SCHEMA_VERSION:
        return False
    if input_hash is not None and cp.input_hash is not None and cp.input_hash != input_hash:
        return False
    if config_hash is not None and cp.config_hash is not None and cp.config_hash != config_hash:
        return False
    if cp.output_data is not None and cp.output_hash is not None:
        if sha256_hex(norm_json(cp.output_data)) != cp.output_hash:
            return False
    return True


async def validate_checkpoint(
    cp: JobCheckpoint,
    resolve: Callable[[str], Awaitable[tuple[str, str] | None]],
) -> str:
    """校验已成功 Checkpoint 是否仍可复用。

    ``resolve(output_ref)`` 重新读取产物，返回 ``(规范化产物文本, schema_version)``；
    引用不可读返回 ``None``。引用损坏 / 摘要不符 / schema 不符均视为 ``checkpoint_corrupt``。
    """
    if cp is None or cp.status != "succeeded":
        return CHECKPOINT_MISSING
    if not cp.output_ref:
        return CHECKPOINT_CORRUPT
    try:
        resolved = await resolve(cp.output_ref)
    except Exception:  # noqa: BLE001
        return CHECKPOINT_CORRUPT
    if resolved is None:
        return CHECKPOINT_CORRUPT
    payload, schema = resolved
    if cp.output_hash is not None and sha256_hex(payload) != cp.output_hash:
        return CHECKPOINT_CORRUPT
    if cp.schema_version is not None and schema != cp.schema_version:
        return CHECKPOINT_CORRUPT
    return CHECKPOINT_OK


# ── 调用记录（S1 计数，S3 网关/预算）────────────────────────────────────
async def record_provider_call(
    db: AsyncSession,
    *,
    operation_key: str,
    attempt_no: int,
    job_id=None,
    checkpoint_id=None,
    subject_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    operation_type: str | None = None,
    input_hash: str | None = None,
    provider_task_id: str | None = None,
    status: str | None = None,
    error_code: str | None = None,
    reserved_amount: int | None = None,
    settled_amount: int | None = None,
    usage_state: str | None = None,
    retry_at=None,
    started_at=None,
) -> ProviderCall:
    """记录一次实际外发操作；同一 (operation_key, attempt_no) 幂等更新。

    S3 接入网关/预算后，``reserved_amount``/``settled_amount``/``usage_state``
    承载该 attempt 的预算预留/结算/计量状态（微元）；``started_at`` 为实际发出时刻。
    """
    row = (
        await db.execute(
            select(ProviderCall).where(
                ProviderCall.operation_key == operation_key,
                ProviderCall.attempt_no == attempt_no,
            )
        )
    ).scalars().first()
    if row is None:
        row = ProviderCall(
            operation_key=operation_key,
            attempt_no=attempt_no,
            job_id=job_id,
            checkpoint_id=checkpoint_id,
            subject_id=subject_id,
            provider=provider,
            model=model,
            operation_type=operation_type,
            input_hash=input_hash,
            provider_task_id=provider_task_id,
            status=status,
            error_code=error_code,
            reserved_amount=reserved_amount,
            settled_amount=settled_amount,
            usage_state=usage_state,
            retry_at=retry_at,
            started_at=started_at or _now(),
        )
        db.add(row)
    else:
        row.status = status
        if error_code is not None:
            row.error_code = error_code
        if provider_task_id is not None:
            row.provider_task_id = provider_task_id
        if reserved_amount is not None:
            row.reserved_amount = reserved_amount
        if settled_amount is not None:
            row.settled_amount = settled_amount
        if usage_state is not None:
            row.usage_state = usage_state
        if retry_at is not None:
            row.retry_at = retry_at
        if started_at is not None and row.started_at is None:
            row.started_at = started_at
        row.finished_at = _now()
    await db.flush()
    return row


async def count_provider_calls(db: AsyncSession, operation_key: str) -> int:
    from sqlalchemy import func

    return (
        await db.execute(
            select(func.count(ProviderCall.id)).where(
                ProviderCall.operation_key == operation_key
            )
        )
    ).scalar_one()
