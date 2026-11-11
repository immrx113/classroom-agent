"""任务查询 / 重试 / 取消接口"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.models.index_version import IndexVersion
from app.models.organize import ChunkManifest
from app.models.processing_job import JobCheckpoint
from app.schemas.job import JobResponse
from app.services.access import enforce_writable
from app.services.job_service import cancel_job, get_job_accessible, retry_job
from app.services.organize_service import organize_service
from app.services.rate_limit_service import FAMILY_RETRY, enforce_user_bucket
from app.services.scope import ScopeContext

router = APIRouter(prefix="/jobs", tags=["任务"])


def _status_display(job) -> str:
    """只读可读文案：provider_unknown 明确显示“调用结果待核对”（CA-24）。"""
    if job.error_type == "provider_unknown" or job.wait_reason == "provider_unknown":
        return "调用结果待核对"
    if job.error_type == "budget_exhausted":
        return "预算不足"
    return job.status


async def _job_units(db: AsyncSession, job) -> tuple[int, int]:
    """按任务类型统计已完成/总单元（复用 Checkpoint 计数，不重复外发）。"""
    if job.type == "asr":
        return (1 if job.output_version is not None else 0), 1
    if job.type == "organize":
        total = (
            await db.execute(
                select(func.coalesce(func.max(ChunkManifest.chunk_count), 0)).where(
                    ChunkManifest.job_id == job.id
                )
            )
        ).scalar_one()
        completed = (
            await db.execute(
                select(func.count(JobCheckpoint.id)).where(
                    JobCheckpoint.job_id == job.id,
                    JobCheckpoint.step_key == "organize.extract",
                    JobCheckpoint.status == "succeeded",
                )
            )
        ).scalar_one()
        return int(completed), int(total)
    if job.type == "index":
        completed = (
            await db.execute(
                select(func.count(JobCheckpoint.id)).where(
                    JobCheckpoint.job_id == job.id,
                    JobCheckpoint.step_key == "index.embed",
                    JobCheckpoint.status == "succeeded",
                )
            )
        ).scalar_one()
        total = (
            await db.execute(
                select(func.coalesce(func.sum(IndexVersion.block_count), 0)).where(
                    IndexVersion.course_id == job.resource_id,
                    IndexVersion.status == "active",
                )
            )
        ).scalar_one()
        return int(completed), int(total)
    return 0, 0


def _to_response(job, *, completed_units: int = 0, total_units: int = 0) -> JobResponse:
    retryable = job.error_type in ("rate_limited", "timeout", "provider_error")
    return JobResponse(
        id=job.id,
        type=job.type,
        resource_id=job.resource_id,
        input_version=job.input_version,
        status=job.status,
        stage=job.stage,
        attempt=job.attempt or 0,
        cancel_requested=job.cancel_requested,
        error_code=job.error_code,
        error_type=job.error_type,
        retryable=retryable,
        provider_task_id=job.provider_task_id,
        output_version=job.output_version,
        created_at=job.created_at,
        updated_at=job.updated_at,
        wait_reason=job.wait_reason,
        retry_at=job.retry_at,
        last_successful_stage=job.last_successful_stage,
        completed_units=completed_units,
        total_units=total_units,
        resume_count=job.resume_count or 0,
        status_display=_status_display(job),
    )


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> JobResponse:
    job = await get_job_accessible(db, scope, job_id)
    completed, total = await _job_units(db, job)
    return _to_response(job, completed_units=completed, total_units=total)


@router.get("/{job_id}/run-detail")
async def get_run_detail(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> dict:
    """A6 运行详情：四类分支选择/执行状态/耗时/错误/重试/用量，与底层调用记录对照。"""
    await get_job_accessible(db, scope, job_id)
    return await organize_service.get_run_detail(db, job_id)


@router.post("/{job_id}/retry", response_model=JobResponse)
async def retry(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> JobResponse:
    await get_job_accessible(db, scope, job_id)
    # 回退只读（CA-25）：手动重试是新任务入口，停写期一律 503、不重新排队。
    enforce_writable()
    # 用户入口令牌桶（CA-14）：手动重试消耗入口桶，拒绝时无状态变更。
    await enforce_user_bucket(scope, FAMILY_RETRY)
    job = await retry_job(db, job_id)
    completed, total = await _job_units(db, job)
    return _to_response(job, completed_units=completed, total_units=total)


@router.post("/{job_id}/cancel", response_model=JobResponse)
async def cancel(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> JobResponse:
    await get_job_accessible(db, scope, job_id)
    job = await cancel_job(db, job_id)
    completed, total = await _job_units(db, job)
    return _to_response(job, completed_units=completed, total_units=total)
