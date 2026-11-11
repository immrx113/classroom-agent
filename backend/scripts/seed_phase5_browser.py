"""阶段 5「入口切换与整体回归」浏览器证据的种子（仅建库 + 生成待上传样例文件）。

- 建独立库 ``yuan_resetv1_p5_browser``（alembic head），不触碰活跃库 ``yuan_classroom_local``。
- 生成浏览器要真实上传的样例文件：8s 可解码 WAV 录音 + 带已知标记的 Markdown 课件。
- **不预置课程/课时/转写/笔记**：课程/课时由浏览器从默认首页真实创建，转写走 FakeASRProvider、
  整理走 FakeOrganizeModel、发布走真实 publish 接口，全链路经课堂页面完成（对齐 A7 判据）。
- 确定性 embedding，无任何收费模型调用。

运行（backend 目录）：
  .venv\\Scripts\\python.exe scripts\\seed_phase5_browser.py
"""

import asyncio
import json
import math
import os
import struct
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.update(
    OPENAI_API_KEY="", DASHSCOPE_API_KEY="", PYTHONPATH=".", PYTHONUTF8="1"
)

import asyncpg
from app.config import settings

ROOT = Path(__file__).resolve().parents[1]  # backend/
DB_NAME = "yuan_resetv1_p5_browser"
STATE_PATH = ROOT / "runtime" / "phase5-browser-state.json"

BASE = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]

# 标记词（与 FakeASRProvider 片段、Markdown 课件一致，供「引用对应原文」核对）
MARK_EXAMPLE = "P5EXAMPLE_CALC_2PLUS3"
MARK_KNOWLEDGE = "P5KNOWLEDGE_DEF_ADD"
MARK_KEY = "P5KEYPOINT_CARRY"
MARK_DOC = "P5DOCSTACK_PAGE1"

# 伪造 ASR 片段：带四类整理标记，FakeOrganizeModel 据此产出可核对的结构化笔记。
FAKE_SEGMENTS = [
    {"speaker": "说话人1", "content": "本堂课总纲：掌握加法运算", "start_ms": 0, "end_ms": 1000},
    {"speaker": "说话人1", "content": f"【例题】{MARK_EXAMPLE} 计算 2+3【解】2+3=5", "start_ms": 1200, "end_ms": 2200},
    {"speaker": "说话人1", "content": f"【知识点】加法的定义【适用】{MARK_KNOWLEDGE} 整数相加", "start_ms": 2400, "end_ms": 3200},
    {"speaker": "说话人1", "content": f"【难点】{MARK_KEY} 进位容易出错【依据】课本第三章例题", "start_ms": 3400, "end_ms": 4400},
]

MARKDOWN = (
    f"# 阶段五课件\n\n"
    f"栈的抽象数据类型 {MARK_DOC} 入栈与出栈操作\n\n"
    f"队列先进先出 FIFO\n"
)


def pg_dsn(name: str) -> str:
    return BASE + "/" + name


def asyncpg_dsn(name: str) -> str:
    return BASE.replace("postgresql://", "postgresql+asyncpg://") + "/" + name


async def create_db(name: str) -> None:
    conn = await asyncpg.connect(BASE + "/postgres", timeout=5)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    conn = await asyncpg.connect(pg_dsn(name))
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()


def run_alembic(revision: str, url: str) -> None:
    env = dict(os.environ)
    env.update(DATABASE_URL=url, PYTHONPATH=".", PYTHONUTF8="1",
               OPENAI_API_KEY="", DASHSCOPE_API_KEY="")
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        env=env, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    if r.returncode != 0:
        raise RuntimeError(f"alembic upgrade {revision} failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")


def write_wav(path: Path, seconds: float = 8.0, rate: int = 16000, freq: float = 440.0) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            sample = int(10000 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", sample)
        w.writeframes(bytes(frames))
    return path.stat().st_size


async def main() -> None:
    await create_db(DB_NAME)
    run_alembic("head", asyncpg_dsn(DB_NAME))

    runtime = ROOT / "runtime"
    wav_path = runtime / "phase5-trial.wav"
    md_path = runtime / "phase5-trial.md"
    wav_size = write_wav(wav_path, seconds=8.0)
    md_path.write_text(MARKDOWN, encoding="utf-8")

    state = {
        "db": DB_NAME,
        "wav": str(wav_path),
        "markdown": str(md_path),
        "wav_size": wav_size,
        "fake_segments": FAKE_SEGMENTS,
        "markers": {"example": MARK_EXAMPLE, "knowledge": MARK_KNOWLEDGE,
                     "key": MARK_KEY, "document": MARK_DOC},
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
