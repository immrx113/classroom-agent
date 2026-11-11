"""阶段 5「入口切换与整体回归」浏览器证据专用后端（真实 app.main:app + 独立种子库）。

- 复用 ``app.main:app``（全部路由 + lifespan），连独立种子库 ``yuan_resetv1_p5_browser``。
- ``FakeASRProvider``（可定制标记片段）+ ``FakeOrganizeModel`` + 确定性 embedding：转写/整理/索引
  全程不经真实供应商，绝不触发收费模型。
- 内置后台派发循环（ASR + organize + index），模拟生产 worker 的至少一次投递；浏览器只按正常
  页面流程操作（上传 → 整理 → 发布 → 问答 → 引用回跳），无需调用任何测试后门。

运行（backend 目录）：
  .venv\\Scripts\\python.exe scripts\\serve_phase5_browser.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
DB_NAME = "yuan_resetv1_p5_browser"

state_path = ROOT / "runtime" / "phase5-browser-state.json"
if state_path.exists():
    try:
        DB_NAME = json.loads(state_path.read_text(encoding="utf-8")).get("db", DB_NAME)
    except Exception:
        pass

# 在导入 app（会实例化 settings / engine / 服务单例）之前切库 + 关付费模型 + 确定性嵌入。
try:
    from dotenv import dotenv_values
    env = dotenv_values(ROOT / ".env")
    raw = env.get("DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
except Exception:
    raw = os.environ.get("DATABASE_URL") or ""

if "/" in raw:
    os.environ["DATABASE_URL"] = raw.rsplit("/", 1)[0] + "/" + DB_NAME
os.environ["OPENAI_API_KEY"] = ""
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["INDEX_EMBEDDING_PROVIDER"] = "deterministic"

import uvicorn  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.main import app  # noqa: E402, F401
from app.services.asr_provider import FakeASRProvider, set_provider  # noqa: E402
from app.services.embedding_service import DeterministicEmbedding  # noqa: E402
from app.services.index_service import index_service  # noqa: E402
from app.services.job_service import recover_and_dispatch  # noqa: E402
from app.services.organize_model import FakeOrganizeModel, set_organize_model  # noqa: E402
from app.services.organize_service import organize_service  # noqa: E402

# 可定制标记片段（与 seed_phase5_browser.py 一致），FakeOrganizeModel 据此产出可核对笔记。
fake_asr = FakeASRProvider()
fake_asr.poll_result = "success"
fake_asr.submit_result = "success"
if state_path.exists():
    try:
        segs = json.loads(state_path.read_text(encoding="utf-8")).get("fake_segments")
        if segs:
            fake_asr.segments = segs
    except Exception:
        pass
set_provider(fake_asr)

fake_organize = FakeOrganizeModel()
set_organize_model(fake_organize)


async def _dispatch_loop() -> None:
    embed = DeterministicEmbedding(1024)
    while True:
        try:
            async with async_session_factory() as db:
                await recover_and_dispatch(db, provider=fake_asr)
            async with async_session_factory() as db:
                await organize_service.recover_and_dispatch_organize(db, model=fake_organize)
            async with async_session_factory() as db:
                await index_service.recover_and_dispatch_index(db, embed=embed)
        except Exception:
            # 派发循环失败不终止服务；下一轮重试。
            pass
        await asyncio.sleep(1)


async def main() -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=8787, log_level="warning")
    server = uvicorn.Server(config)
    loop_task = asyncio.create_task(_dispatch_loop())
    try:
        await server.serve()
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except (asyncio.CancelledError, Exception):
            pass


if __name__ == "__main__":
    asyncio.run(main())
