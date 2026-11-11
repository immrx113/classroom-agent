"""CA-18 两独立进程预算竞争 worker。

父进程（verify_checkpoint_ai_control.py）已把 DATABASE_URL 指向隔离测试库并完成迁移；
本进程只做一次 ``reserve_budget``，打印 ``ok`` 或 ``exhausted``，用于证明两个独立
OS 进程抢最后额度时仅一个成功（FOR UPDATE 行锁串行化，不超可用额）。
不调用真实收费接口。
"""
import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True)
    ap.add_argument("--operation-key", required=True)
    ap.add_argument("--attempt-no", type=int, default=1)
    args = ap.parse_args()

    from app.db.session import async_session_factory
    from app.services import call_budget_service as budget
    from app.services.errors import BudgetExhaustedError

    async with async_session_factory() as db:
        try:
            await budget.reserve_budget(
                db,
                attempt_id=uuid.uuid4(),
                operation_key=args.operation_key,
                attempt_no=args.attempt_no,
                subject_id=args.subject,
            )
            await db.commit()
            print("ok", flush=True)
        except BudgetExhaustedError:
            await db.rollback()
            print("exhausted", flush=True)


if __name__ == "__main__":
    asyncio.run(_main())
