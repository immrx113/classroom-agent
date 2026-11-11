"""可观测性诊断（只读，范围隔离）

CA-24：查询本人 job/wait/预算诊断；越权不泄露跨主体预算；指标区分
「成功产物存量」与「实际复用次数」；每次实际外发调用按归属主体审计关联
（含无 job 的 IndexIntent 调用）。

预算账户按当前 ``scope.subject_id`` 读取，绝不返回其他主体账户；
不暴露 OSS 签名 URL、内部路径或密钥。
"""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.models.processing_job import JobCheckpoint, ProcessingJob, ProviderCall
from app.services import call_budget_service as budget
from app.services.scope import ScopeContext

router = APIRouter(prefix="/diagnostics", tags=["诊断"])


@router.get("/status")
async def diagnostic_status(
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> dict:
    """当前主体的预算 / 等待 / 复用-请求指标（只读）。"""
    subject_id = scope.subject_id
    budget_state = await budget.available_budget(db, subject_id)

    subject_filter = ProcessingJob.subject_id == subject_id

    async def _count(*where):
        stmt = select(func.count(ProcessingJob.id)).where(subject_filter)
        for w in where:
            stmt = stmt.where(w)
        return (await db.execute(stmt)).scalar_one()

    jobs = {
        "total": await _count(),
        "retry_wait": await _count(ProcessingJob.status == "retry_wait"),
        "unknown": await _count(ProcessingJob.error_type == "provider_unknown"),
        "succeeded": await _count(ProcessingJob.status == "succeeded"),
        "failed": await _count(ProcessingJob.status == "failed"),
    }

    # 成功产物数：本人 job 已成功的 Checkpoint 存量（第一次完成即存在，恢复不新增）。
    successful_checkpoints = (
        await db.execute(
            select(func.count(JobCheckpoint.id)).where(
                JobCheckpoint.status == "succeeded",
                JobCheckpoint.job_id.in_(
                    select(ProcessingJob.id).where(subject_filter)
                ),
            )
        )
    ).scalar_one()

    # 实际复用次数：Checkpoint 复用（reuse_count）+ 网关恢复（resume_count），只增不改写。
    reuse_count = (
        await db.execute(
            select(
                func.coalesce(func.sum(ProcessingJob.reuse_count), 0)
                + func.coalesce(func.sum(ProcessingJob.resume_count), 0)
            ).where(subject_filter)
        )
    ).scalar_one()

    # 实际请求次数：按归属主体审计关联（含无 job 的 IndexIntent 调用），不按 job 子查询漏计。
    provider_requests = (
        await db.execute(
            select(func.count(ProviderCall.id)).where(
                ProviderCall.subject_id == subject_id
            )
        )
    ).scalar_one()

    # 未知调用数：结果未知 / 超期 / 次数耗尽的调用（待人工核对，不自动重发）。
    unknown_calls = (
        await db.execute(
            select(func.count(ProviderCall.id)).where(
                ProviderCall.subject_id == subject_id,
                ProviderCall.status.in_(
                    ("unknown", "deadline_exhausted", "attempt_limit_exhausted")
                ),
            )
        )
    ).scalar_one()

    return {
        "subject_id": subject_id,
        "budget": budget_state,
        "jobs": jobs,
        "metrics": {
            "successful_checkpoints": successful_checkpoints,
            "reuse_count": reuse_count,
            "provider_requests": provider_requests,
            "unknown_calls": unknown_calls,
        },
    }
