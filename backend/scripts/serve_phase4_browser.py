"""阶段 4「问答/检索闭环」浏览器证据专用后端（真实 app.main:app + 独立种子库）。

- 复用 ``app.main:app``（全部路由 + lifespan），连独立种子库 ``yuan_resetv1_p4_browser``。
- 确定性 embedding（不调用收费供应商）；``OPENAI_API_KEY=""`` → 课堂问答走确定性摘录答案，
  绝不触发真实 LLM 付费调用。
- 前端 Vite dev server :5173 已把 /api 代理到 127.0.0.1:8787（见 frontend/vite.config.ts）。

运行（backend 目录）：
  .venv/Scripts/python.exe scripts/serve_phase4_browser.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
DB_NAME = "yuan_resetv1_p4_browser"

# 优先从种子状态取库名，避免与 seed 脚本漂移。
state_path = ROOT / "runtime" / "phase4-browser-state.json"
if state_path.exists():
    try:
        DB_NAME = json.loads(state_path.read_text(encoding="utf-8")).get("db", DB_NAME)
    except Exception:
        pass

# 在导入 app（会实例化 settings / engine / 服务单例）之前切库 + 关付费模型。
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
from app.main import app  # noqa: E402, F401


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
