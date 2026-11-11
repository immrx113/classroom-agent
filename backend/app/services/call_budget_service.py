"""持久预算服务（专项「Checkpoint + AI 调用控制」S3）

- 三类账户：主体日账户（subject_daily）、全局日账户（global_daily）、试用累计账户
  （trial_total），金额定点整数微元（1 元 = 1,000,000 微元），无浮点漂移。
- 固定锁顺序 global → subject → trial，以 ``INSERT ... ON CONFLICT DO NOTHING`` 建
  账户 + ``SELECT ... FOR UPDATE`` 行锁串行化多进程预留（CA-18：不超可用额，先到
  即用，重复结算不重复扣款）。
- 预留同事务建 ``budget_reservations``，``attempt_id`` 唯一；结算按 attempt_id 幂等，
  跨上海午夜结算归原预留账户（不因跨日释放未知费用）。业务日按 Asia/Shanghai 计算，
  存储为上海日（YYYY-MM-DD 字符串映射到 Date）。

保守预留下限 1 元；``usage_state`` 为 unknown/estimated 或「可能已发送」保留整份预留，
confirmed_not_sent 释放预留。金额只增不减的结算语义见 ``settle_budget``。
"""

import logging
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.budget import BudgetAccount, BudgetReservation
from app.services.errors import BudgetExhaustedError

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
# trial_total 不按自然日，用固定哨兵日保证 (kind, account_key, day) 唯一约束可靠去重。
TRIAL_SENTINEL_DAY = date(1970, 1, 1)

KIND_SUBJECT_DAILY = "subject_daily"
KIND_GLOBAL_DAILY = "global_daily"
KIND_TRIAL_TOTAL = "trial_total"

# 预留/结算锁定顺序（global → subject → trial，避免死锁）
_ACCOUNT_SPECS = (KIND_GLOBAL_DAILY, KIND_SUBJECT_DAILY, KIND_TRIAL_TOTAL)


def micro_per_cny() -> int:
    return settings.BUDGET_MICRO_PER_CNY


def reserve_min_micro() -> int:
    return settings.BUDGET_RESERVE_MIN_CNY * micro_per_cny()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def shanghai_day(dt: datetime | None = None) -> date:
    """Asia/Shanghai 业务日（存储 UTC，结算/预留按上海日界划分）。"""
    return (dt or _now()).astimezone(SHANGHAI).date()


def _account_key(kind: str, subject_id: str) -> str:
    return "global" if kind == KIND_GLOBAL_DAILY else subject_id


def _cap_micro(kind: str) -> int:
    if kind == KIND_SUBJECT_DAILY:
        return settings.BUDGET_SUBJECT_DAILY_CNY * micro_per_cny()
    if kind == KIND_GLOBAL_DAILY:
        return settings.BUDGET_GLOBAL_DAILY_CNY * micro_per_cny()
    return settings.BUDGET_TRIAL_TOTAL_CNY * micro_per_cny()


def _day_for(kind: str, day: date) -> date:
    return TRIAL_SENTINEL_DAY if kind == KIND_TRIAL_TOTAL else day


async def _ensure_accounts(db: AsyncSession, subject_id: str, day: date) -> None:
    """建缺失账户（ON CONFLICT DO NOTHING 去重）；随后调用方 FOR UPDATE 串行化。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    for kind in _ACCOUNT_SPECS:
        stmt = pg_insert(BudgetAccount).values(
            kind=kind,
            account_key=_account_key(kind, subject_id),
            day=_day_for(kind, day),
            cap_micro=_cap_micro(kind),
            reserved_micro=0,
            settled_micro=0,
        ).on_conflict_do_nothing(
            index_elements=["kind", "account_key", "day"]
        )
        await db.execute(stmt)


async def _lock_accounts(db: AsyncSession, subject_id: str, day: date) -> list[BudgetAccount]:
    """按固定顺序锁定三类账户（FOR UPDATE），返回锁定后的账户行。"""
    rows: list[BudgetAccount] = []
    for kind in _ACCOUNT_SPECS:
        row = (
            await db.execute(
                select(BudgetAccount)
                .where(
                    BudgetAccount.kind == kind,
                    BudgetAccount.account_key == _account_key(kind, subject_id),
                    BudgetAccount.day == _day_for(kind, day),
                )
                .with_for_update()
            )
        ).scalars().first()
        if row is None:  # 理论不可达（_ensure_accounts 已建），防御性抛错
            raise RuntimeError(f"预算账户缺失: {kind}")
        rows.append(row)
    return rows


async def reserve_budget(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    operation_key: str,
    attempt_no: int,
    subject_id: str,
    amount_micro: int | None = None,
    day: date | None = None,
) -> int:
    """预留一次外部 attempt 的预算；可用额不足抛 ``BudgetExhaustedError``（不出网）。

    返回实际预留微元数（= 每个账户均扣除的金额，取 amount_micro 与保守下限的较大者）。
    """
    amount_micro = amount_micro or reserve_min_micro()
    if amount_micro < reserve_min_micro():
        amount_micro = reserve_min_micro()
    day = day or shanghai_day()

    await _ensure_accounts(db, subject_id, day)
    accounts = await _lock_accounts(db, subject_id, day)

    # 可用额 = 上限 - 在途预留 - 已结算消耗；结算/未知保守保留计入已消耗，
    # 使日上限/试用累计上限真正约束总额（CA-18 不因换入口绕总额，CA-19 累计不重置）。
    available = min(acct.cap_micro - acct.reserved_micro - acct.settled_micro for acct in accounts)
    if available < amount_micro:
        # 预算不足：停止新调用，不预留、不建调用记录、不出网（CA-18）
        await db.rollback()
        raise BudgetExhaustedError(
            f"预算可用额度不足（可用 {available} 微元 < 需 {amount_micro} 微元）"
        )

    for acct in accounts:
        acct.reserved_micro += amount_micro

    db.add(
        BudgetReservation(
            attempt_id=attempt_id,
            operation_key=operation_key,
            attempt_no=attempt_no,
            subject_id=subject_id,
            reserved_micro=amount_micro,
            settled_micro=0,
            usage_state=None,
            status="reserved",
            day=day,
        )
    )
    await db.flush()
    return amount_micro


async def settle_budget(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    settled_micro: int,
    usage_state: str,
) -> None:
    """按 attempt_id 幂等结算：释放预留、按已发生用量计入 settled（归原日账户）。

    ``settled_micro`` 为本次确认的已发生费用（confirmed_not_sent 时为 0，释放整份预留；
    unknown/estimated 时等于预留整份，保留保守预留）。重复调用不重复扣款（CA-18/CA-19）。
    """
    res = (
        await db.execute(
            select(BudgetReservation)
            .where(BudgetReservation.attempt_id == attempt_id)
            .with_for_update()
        )
    ).scalars().first()
    if res is None:
        return  # 无预留记录，幂等返回
    if res.status in ("settled", "released"):
        return  # 已结算/已释放：不重复扣款

    settled_micro = max(0, min(settled_micro, res.reserved_micro))
    # 锁定原预留日账户（结算归原日，不因跨午夜释放到新日）
    accounts = await _lock_accounts(db, res.subject_id, res.day)
    for acct in accounts:
        acct.reserved_micro = max(0, acct.reserved_micro - res.reserved_micro)
        acct.settled_micro += settled_micro

    res.settled_micro = settled_micro
    res.usage_state = usage_state
    res.status = "released" if settled_micro == 0 else "settled"
    res.settled_at = _now()
    await db.flush()


async def available_budget(db: AsyncSession, subject_id: str, day: date | None = None) -> dict:
    """诊断：返回三类账户的可用/预留/结算（微元），供可观测性与验收断言。"""
    day = day or shanghai_day()
    out: dict = {}
    for kind in _ACCOUNT_SPECS:
        row = (
            await db.execute(
                select(BudgetAccount).where(
                    BudgetAccount.kind == kind,
                    BudgetAccount.account_key == _account_key(kind, subject_id),
                    BudgetAccount.day == _day_for(kind, day),
                )
            )
        ).scalars().first()
        out[kind] = {
            "available_micro": (row.cap_micro - row.reserved_micro - row.settled_micro) if row else _cap_micro(kind),
            "reserved_micro": row.reserved_micro if row else 0,
            "settled_micro": row.settled_micro if row else 0,
            "cap_micro": row.cap_micro if row else _cap_micro(kind),
        }
    return out


async def migrate_legacy_trial(db: AsyncSession, subject_id: str) -> int:
    """迁移旧 SQLite 试用账本到 trial_total 累计账户（CA-19）。

    旧账本每行 ``reserved_cny`` 为保守结算上限（元，可能小数）；按整数微元保守向上取整
    汇总为已结算金额，写入 trial_total 账户的 ``settled_micro``，不重置累计额度、不删除旧记录。
    返回迁移的已结算总微元。
    """
    path = Path(settings.CLASSROOM_TRIAL_LEDGER)
    total_micro = 0
    if path.exists():
        with closing(sqlite3.connect(path, timeout=10)) as conn:
            try:
                rows = conn.execute(
                    "SELECT coalesce(sum(reserved_cny),0) FROM calls"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = [(0,)]
        total_micro = _cny_to_micro(float(rows[0][0]))

    await _ensure_accounts(db, subject_id, shanghai_day())
    account = (
        await db.execute(
            select(BudgetAccount)
            .where(
                BudgetAccount.kind == KIND_TRIAL_TOTAL,
                BudgetAccount.account_key == _account_key(KIND_TRIAL_TOTAL, subject_id),
                BudgetAccount.day == TRIAL_SENTINEL_DAY,
            )
            .with_for_update()
        )
    ).scalars().first()
    account.settled_micro = max(account.settled_micro, total_micro)
    await db.flush()
    return total_micro


def _cny_to_micro(cny: float) -> int:
    """元 → 整数微元（保守向上取整，避免小数截断少计）。"""
    import math

    return int(math.ceil(cny * micro_per_cny()))


# 进程内迁移标记：按 (subject_id, ledger_path, mtime_ns) 去重；账本路径或内容变化即重新迁移。
_migrated_ledger: set[tuple[str, str, int]] = set()


def _ledger_signature(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


async def ensure_legacy_trial_migrated(subject_id: str) -> None:
    """在首次预留前把旧 SQLite 试用账本迁入 trial_total（CA-19）。

    用独立短事务执行，避免与 ``reserve_budget`` 的 global→subject→trial 锁序在同一
    事务内交错；幂等（``migrate_legacy_trial`` 以 max 合并），按账本签名去重。
    """
    path = Path(settings.CLASSROOM_TRIAL_LEDGER)
    sig = (subject_id, str(path), _ledger_signature(path))
    if sig in _migrated_ledger:
        return
    from app.db.session import async_session_factory

    async with async_session_factory() as mdb:
        try:
            await migrate_legacy_trial(mdb, subject_id)
            await mdb.commit()
        except Exception:
            await mdb.rollback()
            raise
    _migrated_ledger.add(sig)
