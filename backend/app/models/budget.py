"""持久预算账户、预留与供应商并发 permit（专项「Checkpoint + AI 调用控制」S3）

- 金额定点整数微元（1 元 = 1,000,000 微元），不用浮点（CA-18 无浮点漂移）。
- ``budget_accounts``：主体日账户 / 全局日账户 / 试用累计账户。日账户按
  Asia/Shanghai 业务日划分（存储 UTC，结算归原预留账户，跨午夜不重置）。
- ``budget_reservations``：以 ``attempt_id`` 唯一预留/结算，重复回执不重复扣款。
- ``provider_permit_slots``：固定槽位数（= CHECKPOINT_PROVIDER_PERMIT_MAX）的
  数据库并发 permit，``SELECT ... FOR UPDATE SKIP LOCKED`` 原子抢占，绑定任务租约，
  过期可回收（CA-17）。
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BudgetAccount(Base):
    """单一预算账户：kind × account_key × day 唯一。reserved/settled 为累计微元。"""

    __tablename__ = "budget_accounts"
    __table_args__ = (
        UniqueConstraint("kind", "account_key", "day", name="uq_budget_account_kind_key_day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # subject_daily / global_daily / trial_total
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    # subject_daily/trial_total → subject_id；global_daily → "global"
    account_key: Mapped[str] = mapped_column(String(100), nullable=False)
    # Asia/Shanghai 业务日（YYYY-MM-DD）；trial_total 用固定哨兵日（累计，不按自然日）。
    # 非空保证 (kind, account_key, day) 唯一约束在并发 INSERT ON CONFLICT 下可靠去重。
    day: Mapped[date] = mapped_column(Date, nullable=False)
    cap_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    settled_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BudgetReservation(Base):
    """一次外部 attempt 的预留/结算记录，``attempt_id`` 唯一（幂等结算）。"""

    __tablename__ = "budget_reservations"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_budget_reservations_attempt"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(300), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False)
    reserved_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    settled_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # unknown / estimated / confirmed_sent / confirmed_not_sent
    usage_state: Mapped[str | None] = mapped_column(String(50))
    # reserved / settled / released
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="reserved")
    # 预留时的业务日（结算归原日账户，跨午夜不释放到新日；trial 为哨兵日）
    day: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderPermitSlot(Base):
    """供应商在途并发 permit 的固定槽位（CA-17，最多 2 个在途）。"""

    __tablename__ = "provider_permit_slots"
    __table_args__ = (
        UniqueConstraint("slot", name="uq_provider_permit_slots_slot"),
    )

    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
