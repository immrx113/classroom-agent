"""供应商在途并发 permit（专项 CA-17）

固定槽位数 = ``CHECKPOINT_PROVIDER_PERMIT_MAX``（默认 2），以
``SELECT ... FOR UPDATE SKIP LOCKED`` 原子抢占空闲/过期槽位；同一时刻在途有效
permit ≤ 2。permit 绑定任务租约（owner/epoch/过期时间），租约过期后槽位可被回收，
取消/响应后释放。等待不占数据库长锁、不占 permit（CA-17）。
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.budget import ProviderPermitSlot

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ttl() -> timedelta:
    return timedelta(seconds=settings.CHECKPOINT_PROVIDER_PERMIT_TTL_SECONDS)


async def ensure_slots(db: AsyncSession) -> None:
    """确保槽位行存在（迁移会建，这里幂等兜底）。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    for slot in range(settings.CHECKPOINT_PROVIDER_PERMIT_MAX):
        stmt = pg_insert(ProviderPermitSlot).values(
            slot=slot, lease_owner=None, lease_epoch=0, lease_expires_at=None
        ).on_conflict_do_nothing(index_elements=["slot"])
        await db.execute(stmt)


async def acquire_permit(
    db: AsyncSession, *, job_id: uuid.UUID, owner: str, epoch: int, operation_key: str
) -> int | None:
    """抢占一个空闲/过期 permit 槽位；无可用槽返回 None（调用方控制等待）。

    返回槽位号；随后外部调用期间槽位保持占用，结束后 ``release_permit``。
    过期槽位（worker 失联）可被回收复用。
    """
    await ensure_slots(db)
    now = _now()
    slot_row = (
        await db.execute(
            select(ProviderPermitSlot)
            .where(
                (ProviderPermitSlot.lease_expires_at.is_(None))
                | (ProviderPermitSlot.lease_expires_at < now)
            )
            .order_by(ProviderPermitSlot.slot)
            .with_for_update(skip_locked=True)
        )
    ).scalars().first()
    if slot_row is None:
        await db.rollback()
        return None
    slot_row.job_id = job_id
    slot_row.lease_owner = owner
    slot_row.lease_epoch = epoch
    slot_row.lease_expires_at = now + _ttl()
    slot_row.operation_key = operation_key
    await db.flush()
    return slot_row.slot


async def release_permit(db: AsyncSession, *, job_id: uuid.UUID, owner: str, epoch: int) -> None:
    """释放本执行者持有的 permit 槽位（取消/响应后调用）。"""
    await db.execute(
        update(ProviderPermitSlot)
        .where(
            ProviderPermitSlot.job_id == job_id,
            ProviderPermitSlot.lease_owner == owner,
            ProviderPermitSlot.lease_epoch == epoch,
        )
        .values(job_id=None, lease_owner=None, lease_epoch=0, lease_expires_at=None, operation_key=None)
    )


async def renew_permit(db: AsyncSession, *, job_id: uuid.UUID, owner: str, epoch: int) -> bool:
    """续租本执行者的 permit 槽位（长调用期间与任务租约一并心跳）。"""
    now = _now()
    result = await db.execute(
        update(ProviderPermitSlot)
        .where(
            ProviderPermitSlot.job_id == job_id,
            ProviderPermitSlot.lease_owner == owner,
            ProviderPermitSlot.lease_epoch == epoch,
        )
        .values(lease_expires_at=now + _ttl())
    )
    return result.rowcount == 1


async def renew_loop(job_id: uuid.UUID | None, owner: str, epoch: int, stop) -> None:
    """长调用期间独立 session 周期续租 permit 槽位（CA-17）；失去槽位即停止。

    与 ``lease_service.heartbeat_loop`` 同理：主 session 阻塞在单次 transport 时，
    用独立 session 续租，避免槽位因超过 TTL 被回收；续租失败（槽位已被回收/接管）
    即自行退出。短调用在 stop 前不会开启任何连接。
    """
    import asyncio

    from app.db.session import async_session_factory

    interval = max(1.0, settings.CHECKPOINT_PROVIDER_PERMIT_TTL_SECONDS / 2.0)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return
        async with async_session_factory() as hb_db:
            try:
                ok = await renew_permit(hb_db, job_id=job_id, owner=owner, epoch=epoch)
                await hb_db.commit()
            except Exception:  # noqa: BLE001
                await hb_db.rollback()
                continue
            if not ok:
                return  # 槽位已被回收/接管，停止续租


async def count_in_flight(db: AsyncSession) -> int:
    """诊断：当前在途有效 permit 数（≤ max）。"""
    from sqlalchemy import func

    now = _now()
    return (
        await db.execute(
            select(func.count(ProviderPermitSlot.slot)).where(
                ProviderPermitSlot.lease_expires_at.is_not(None),
                ProviderPermitSlot.lease_expires_at >= now,
            )
        )
    ).scalar_one()
