r"""Phase 04 受 CA-11 claim 语义影响的 P04-12 / P04-13 复验。

不重开全部历史验收：只运行两条受影响条款，故障模拟已显式过期租约
（原执行者失联后租约到期，恢复者方可接管）。证据独立保存到
evidence-phase4-20261029/phase4-affected/。

运行（backend 目录）：
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  .\.venv\Scripts\python.exe scripts/verify_phase4_affected.py
"""

import asyncio
import json
import sys

import verify_phase4_frozen as p4  # noqa: E402  导入即完成隔离库指向（不触碰 yuan_meet）
from httpx import ASGITransport, AsyncClient  # noqa: E402

OUT = p4.OUT / "phase4-affected"
OUT.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    await p4.create_db(p4.MAIN_DB)
    p4.run_alembic("head", p4.asyncpg_dsn(p4.MAIN_DB))

    current = {"subject_id": p4.SUBJECT_A, "scope": None}

    async def override_scope():
        if current["scope"] is not None:
            return current["scope"]
        return p4.ScopeContext(subject_id=current["subject_id"])

    p4.app.dependency_overrides[p4.get_scope] = override_scope
    try:
        async with AsyncClient(transport=ASGITransport(app=p4.app), base_url="http://test") as client:
            for pid, coro in [
                ("P04-12", p4.p04_12(current, client)),
                ("P04-13", p4.p04_13(current, client)),
            ]:
                try:
                    await coro
                except Exception as e:  # noqa: BLE001
                    import traceback
                    tb = traceback.format_exc()
                    (OUT / f"_trace_{pid}.txt").write_text(tb, encoding="utf-8")
                    p4.record(pid, "脚本异常", False, {"error": repr(e), "trace": tb})
    finally:
        p4.app.dependency_overrides.clear()

    failed = [r for r in p4.RESULTS if r["result"] != "通过"]
    print("=" * 70)
    print(f"TOTAL {len(p4.RESULTS)}  PASS {len(p4.RESULTS) - len(failed)}  FAIL {len(failed)}")
    (OUT / "results.json").write_text(
        json.dumps(p4.RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    for name in p4.DBS:
        await p4.drop_db(name)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
