r"""S2 令牌桶独立进程 worker（CA-13 两进程共享桶证据）。

由 verify_checkpoint_ai_control.py 以子进程启动：连接同一真实 Redis 测试库，
对给定桶 key 消费 N 次，输出「放行次数」。父进程读取 stdout 判定跨进程共享额度。

用法（backend 目录）：
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"; $env:REDIS_URL="redis://localhost:6379/15"
  .\.venv\Scripts\python.exe scripts/s2_redis_worker.py --key rl:v1:... --capacity 3 --refill 0.1 --count 3
"""

import argparse
import asyncio
import os

os.environ.setdefault("PYTHONPATH", ".")
os.environ.setdefault("PYTHONUTF8", "1")

from app.services import rate_limit_service as rl  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="完整桶 key（父进程已含 policy_version/scope_hash）")
    ap.add_argument("--capacity", type=float, required=True)
    ap.add_argument("--refill", type=float, required=True)
    ap.add_argument("--count", type=int, required=True, help="连续消费次数")
    ap.add_argument("--cost", type=float, default=1.0)
    args = ap.parse_args()

    await rl.init_pool()
    try:
        allowed = 0
        for _ in range(args.count):
            result = await rl.consume(args.key, args.capacity, args.refill, args.cost)
            if result.allowed:
                allowed += 1
        print(allowed)
    finally:
        await rl.close_pool()
    return 0


if __name__ == "__main__":
    asyncio.run(main())
