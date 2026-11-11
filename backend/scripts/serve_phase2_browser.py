"""阶段 2 复验「浏览器证据」专用后端（可控替身 ASR，按需派发）。

复用 ``app.main:app``（全部路由 + lifespan），连活跃库 ``yuan_classroom_local``。
- ``set_provider(FakeASRProvider())``：新上传走可控替身，绝不触发真实 DashScope 收费。
- 不后台循环：ASR 派发由测试脚本显式调用 ``POST /__test/dispatch`` 触发一次
  ``recover_and_dispatch``（type=asr，仅 ASR，不触发 organize / index），
  从而精确控制「失败 → 重试」时序，避免自动重试与手动重试竞态。
- 控制端点：``POST /__test/asr-fail`` / ``asr-ok`` 切换替身 poll 结果。

运行（backend 目录）：python scripts/serve_phase2_browser.py
（前端 Vite 已把 /api 代理到 127.0.0.1:8787）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI

from app.db.session import async_session_factory
from app.main import app  # noqa: F401  (lifespan: init rate_limit pool)
from app.services.asr_provider import FakeASRProvider, set_provider
from app.services.job_service import recover_and_dispatch

fake = FakeASRProvider()
set_provider(fake)


@app.post("/__test/asr-fail")
async def asr_fail():
    fake.submit_result = "success"
    fake.poll_result = "failure"
    return {"poll_result": fake.poll_result, "submit_result": fake.submit_result}


@app.post("/__test/asr-ok")
async def asr_ok():
    fake.submit_result = "success"
    fake.poll_result = "success"
    return {"poll_result": fake.poll_result, "submit_result": fake.submit_result}


@app.get("/__test/asr-state")
async def asr_state():
    return {"poll_result": fake.poll_result, "submit_result": fake.submit_result}


@app.post("/__test/dispatch")
async def dispatch_once():
    """按需派发一次 ASR（不触发 organize/index），返回派发统计。"""
    async with async_session_factory() as db:
        stats = await recover_and_dispatch(db, provider=fake)
    return stats


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
