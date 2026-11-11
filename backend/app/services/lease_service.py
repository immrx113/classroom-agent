"""任务租约与原子领取（专项 CA-11）

- ``claim_job``：以 ``SELECT ... FOR UPDATE SKIP LOCKED`` 原子领取任务，写入
  owner / epoch / lease_expires_at，同一时刻仅一个有效 owner。
- ``renew_lease``：心跳续租，健康长调用不被误接管。
- ``assert_owner``：产物提交 / active 切换前校验 owner+epoch，过期执行者不得写入。
- ``expire_stale_leases``：把租约过期的 running 任务重入队，供恢复者接管。
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.processing_job import ProcessingJob

logger = logging.getLogger(__name__)


class StaleLeaseError(Exception):
    """当前执行者租约已过期或被接管，禁止新调用 / 提交产物。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def lease_ttl() -> timedelta:
    return timedelta(seconds=settings.CHECKPOINT_LEASE_SECONDS)


async def claim_job(
    db: AsyncSession, job_id, owner: str, now: datetime | None = None
) -> tuple[bool, int]:
    """原子领取：成功返回 (True, epoch)，已被他人有效持有返回 (False, 0)。

    行锁 + SKIP LOCKED 保证两个独立进程竞争时只有一个取得所有权；
    终态（succeeded/cancelled/failed）不可领取。
    """
    now = now or _now()
    row = (
        await db.execute(
            select(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .execution_options(populate_existing=True)
            .with_for_update(skip_locked=True)
        )
    ).scalars().first()
    if row is None:
        return False, 0
    if row.status in ("succeeded", "cancelled", "failed"):
        return False, 0
    if (
        row.status == "running"
        and row.lease_expires_at is not None
        and row.lease_expires_at >= now
    ):
        # 已有有效 owner（或本进程自身持有），不可重复领取
        return False, 0
    if row.status in ("queued", "retry_wait"):
        row.status = "running"
        row.attempt = (row.attempt or 0) + 1
    row.lease_owner = owner
    row.lease_epoch = (row.lease_epoch or 0) + 1
    row.lease_expires_at = now + lease_ttl()
    row.heartbeat_at = now
    row.wait_reason = None
    await db.flush()
    return True, row.lease_epoch


async def renew_lease(
    db: AsyncSession, job_id, owner: str, epoch: int, now: datetime | None = None
) -> bool:
    """心跳续租：仅当前 owner+epoch 有效且未过期时续期（原子条件 UPDATE）。

    不读 session identity-map 缓存；过期 / 被接管的旧 owner 续租会被条件过滤，
    ``rowcount`` 为 0，避免旧执行者用缓存值误续（CA-11）。
    """
    now = now or _now()
    result = await db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.id == job_id,
            ProcessingJob.lease_owner == owner,
            ProcessingJob.lease_epoch == epoch,
            or_(
                ProcessingJob.lease_expires_at.is_(None),
                ProcessingJob.lease_expires_at >= now,
            ),
        )
        .values(lease_expires_at=now + lease_ttl(), heartbeat_at=now)
    )
    return result.rowcount == 1


async def assert_owner(
    db: AsyncSession, job_id, owner: str, epoch: int, *, for_update: bool = False
) -> bool:
    """提交前校验：当前 owner+epoch 仍有效（未过期、未被接管）。

    ``for_update=True`` 时对任务行加 ``FOR UPDATE`` 锁，锁保持到本事务提交/回滚，
    使「校验归属 → 写产物 → 提交」在同一事务内原子（CA-11：不先查 bool 再无条件提交）。
    均绕过 identity-map 缓存（populate_existing），读到另一 session 接管后的最新归属。
    """
    now = _now()
    stmt = select(ProcessingJob).where(ProcessingJob.id == job_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (
        await db.execute(stmt.execution_options(populate_existing=True))
    ).scalars().first()
    if row is None or row.lease_owner != owner or row.lease_epoch != epoch:
        return False
    if row.lease_expires_at is not None and row.lease_expires_at < now:
        return False
    return True


async def mutate_status_if_owner(
    db: AsyncSession,
    job_id,
    owner: str,
    epoch: int,
    mutate: Callable[[ProcessingJob], None],
) -> bool:
    """执行者触发的状态写入受租约保护（CA-11：迟到异常不得改写新 owner 状态）。

    先回滚当前（可能已失败）事务，再加行锁重新读取并核对 owner/epoch/未过期；
    仅当仍为本执行者时应用 ``mutate`` 并提交。失去租约返回 False（已回滚，未写入）。
    用户显式取消 / 运维操作有独立通道，不经此 helper，避免误拦用户取消。
    """
    await db.rollback()
    row = (
        await db.execute(
            select(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalars().first()
    now = _now()
    if (
        row is None
        or row.lease_owner != owner
        or row.lease_epoch != epoch
        or (row.lease_expires_at is not None and row.lease_expires_at < now)
    ):
        await db.rollback()
        return False
    mutate(row)
    await db.commit()
    return True


async def heartbeat_loop(job_id, owner: str, epoch: int, stop) -> None:
    """长调用期间独立 session 周期续租（CA-11）；失去租约即停止。

    organize / ASR / index 三入口的外发调用期间共享此续租，避免主 session 阻塞时
    租约过期被接管；``renew_lease`` 返回 False（已被接管/过期）即自行退出。
    """
    import asyncio

    from app.db.session import async_session_factory

    interval = settings.CHECKPOINT_HEARTBEAT_SECONDS
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return
        async with async_session_factory() as hb_db:
            try:
                ok = await renew_lease(hb_db, job_id, owner, epoch)
                await hb_db.commit()
            except Exception:  # noqa: BLE001
                await hb_db.rollback()
                continue
            if not ok:
                return  # 已失去租约，停止续租


async def expire_stale_leases(db: AsyncSession, job_type: str | None = None) -> list[ProcessingJob]:
    """把租约过期的 running 任务重入队，返回被重入队的任务列表。"""
    now = _now()
    q = select(ProcessingJob).where(
        ProcessingJob.status == "running",
        ProcessingJob.lease_expires_at.is_not(None),
        ProcessingJob.lease_expires_at < now,
    )
    if job_type is not None:
        q = q.where(ProcessingJob.type == job_type)
    rows = (await db.execute(q)).scalars().all()
    for row in rows:
        row.status = "queued"
        row.wait_reason = None
    await db.flush()
    return list(rows)


def new_owner() -> str:
    """本进程/本执行者的一次性标识（不与凭证或主体身份混用）。"""
    return f"worker-{uuid.uuid4().hex[:12]}"
