"""阶段 2「课程、课时与资料输入」A1 / A2 自动验证（可控替身路径）。

对应 docs/classroom-agent/reset-v1/03-acceptance.md：
- A1 / R1：创建课程及两个课时，刷新；另一用户用已知标识直接读写。
  → 归属和数量正确，跨用户请求被拒且内容未改变；保存请求结果及前后快照。
- A2 / R2：上传真实录音与课件，点选转写段；模拟一次转写失败后重试。
  → 有序时间段（回听定位）、课件可定位原段、失败可见、重试得到有效结果。

本脚本是「模拟结果」：隔离测试库 + ASGITransport（进程内 HTTP）+ FakeASRProvider
可控替身 + 跨用户 subject 注入。真实供应商（DashScope/OSS）链路结果见交接文档，
本脚本不冒充真实调用。

运行（backend 目录）：
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  python scripts/verify_phase2_classroom_input.py
"""

import asyncio
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

os.environ.update(
    OPENAI_API_KEY="", DASHSCOPE_API_KEY="", PYTHONPATH=".", PYTHONUTF8="1"
)

import asyncpg
from app.config import settings

BASE = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
PREFIX = "yuan_resetv1_phase2_test_" + uuid.uuid4().hex[:10] + "_"
MAIN_DB = PREFIX + "main"

# 在导入会创建引擎的模块前，把引擎指向隔离测试库，绝不触碰活跃库 yuan_classroom_local。
settings.DATABASE_URL = BASE.replace("postgresql://", "postgresql+asyncpg://") + "/" + MAIN_DB
os.environ["DATABASE_URL"] = settings.DATABASE_URL

from app.api.deps import get_scope
from app.db.session import async_session_factory
from app.main import app
from app.models.audio_asset import AudioAsset
from app.models.course import Course
from app.models.meeting import Meeting
from app.models.processing_job import ProcessingJob
from app.models.transcript_version import TranscriptSegment, TranscriptVersion
from app.services.asr_provider import FakeASRProvider, clear_provider_override, set_provider
from app.services.audio_asset_service import audio_asset_service
from app.services.job_service import recover_and_dispatch, run_job
from app.services.scope import ScopeContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]  # backend/
OUT = ROOT.parent / "docs" / "classroom-agent" / "reset-v1" / "evidence" / "phase2"
OUT.mkdir(parents=True, exist_ok=True)

SUBJECT_A = "subject-a"
SUBJECT_B = "subject-b"
RESULTS: list[dict] = []

TEST_UPLOAD_DIR = ROOT / "uploads_resetv1_phase2" / uuid.uuid4().hex[:8]
settings.UPLOAD_DIR = str(TEST_UPLOAD_DIR)

MATERIALS = ROOT.parent / "docs" / "classroom-agent" / "trial-materials"
TRIAL_WAV = MATERIALS / "newton-trial.wav"
TRIAL_MD = MATERIALS / "newton-trial.md"


def record(pid: str, check: str, ok: bool, detail) -> None:
    row = dict(id=pid, check=check, result="通过" if ok else "不通过", detail=detail)
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


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


async def drop_db(name: str) -> None:
    conn = await asyncpg.connect(BASE + "/postgres", timeout=5)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


def run_alembic(revision: str, url: str) -> None:
    env = dict(os.environ)
    env.update(
        DATABASE_URL=url, PYTHONPATH=".", PYTHONUTF8="1",
        OPENAI_API_KEY="", DASHSCOPE_API_KEY="",
    )
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        env=env, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    if r.returncode != 0:
        raise RuntimeError(f"alembic upgrade {revision} failed:\n{r.stdout}\n{r.stderr}")


def make_wav(duration_s: float, freq: float = 440.0, sr: int = 16000) -> bytes:
    n = int(sr * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / sr))) for i in range(n)
        )
        w.writeframes(frames)
    return buf.getvalue()


def _read_material(name: str) -> bytes:
    p = MATERIALS / name
    if p.exists():
        return p.read_bytes()
    return None


# ── A1：归属 + 跨用户 ──────────────────────────────────────────────
async def a1(current, client):
    current["subject_id"] = SUBJECT_A

    r_course = await client.post("/api/courses", json={"title": "验收课程-牛顿", "teacher": "张老师", "term": "2026秋"})
    course_ok = r_course.status_code == 201
    course_id = r_course.json().get("id")
    course_title = r_course.json().get("title")

    r_l1 = await client.post(f"/api/courses/{course_id}/lectures", json={"title": "第 1 讲 牛顿第二定律", "lecture_date": "2026-11-10"})
    r_l2 = await client.post(f"/api/courses/{course_id}/lectures", json={"title": "第 2 讲 例题精讲"})
    l1_id, l2_id = r_l1.json().get("id"), r_l2.json().get("id")
    created_ok = course_ok and r_l1.status_code == 201 and r_l2.status_code == 201

    # 刷新后数量正确（课程列表含 lecture_count）
    r_list = await client.get("/api/courses")
    items = r_list.json().get("items", [])
    mine = next((c for c in items if c["id"] == course_id), None)
    count_ok = mine is not None and mine.get("lecture_count") == 2

    r_lectures = await client.get(f"/api/courses/{course_id}/lectures")
    lectures_a = r_lectures.json()
    before = sorted((l["id"], l["title"]) for l in lectures_a)

    # 跨用户：B 用已知标识直接读写 → 拒绝且内容未改变
    current["subject_id"] = SUBJECT_B
    r_b_list = await client.get("/api/courses")
    b_leak = any(c["id"] == course_id for c in r_b_list.json().get("items", []))
    r_b_lectures = await client.get(f"/api/courses/{course_id}/lectures")
    r_b_detail = await client.get(f"/api/lectures/{l1_id}")
    r_b_transcript = await client.get(f"/api/lectures/{l1_id}/transcript")
    r_b_write = await client.post(f"/api/courses/{course_id}/lectures", json={"title": "越权课时"})
    r_b_delete = await client.delete(f"/api/courses/{course_id}")
    r_b_audio = await client.post(
        f"/api/lectures/{l1_id}/audio",
        files={"file": ("evil.wav", make_wav(1.0), "audio/wav")},
    )

    # 回 A：内容未改变
    current["subject_id"] = SUBJECT_A
    r_after = await client.get(f"/api/courses/{course_id}/lectures")
    after = sorted((l["id"], l["title"]) for l in r_after.json())

    denied = (
        not b_leak
        and r_b_lectures.status_code == 404
        and r_b_detail.status_code == 404
        and r_b_transcript.status_code == 404
        and r_b_write.status_code == 404
        and r_b_delete.status_code == 404
        and r_b_audio.status_code == 404
    )
    unchanged = before == after and len(after) == 2

    record(
        "A1",
        "创建课程+两课时归属数量正确；跨用户读写被拒且内容未改变",
        created_ok and count_ok and denied and unchanged,
        dict(
            course_status=r_course.status_code, course_title=course_title,
            lecture1=r_l1.status_code, lecture2=r_l2.status_code,
            lecture_count=mine.get("lecture_count") if mine else None,
            b_leak=b_leak,
            b_lectures=r_b_lectures.status_code, b_detail=r_b_detail.status_code,
            b_transcript=r_b_transcript.status_code, b_write=r_b_write.status_code,
            b_delete=r_b_delete.status_code, b_audio=r_b_audio.status_code,
            before_snapshot=before, after_snapshot=after,
        ),
    )
    return course_id, l1_id, l2_id


# ── A2：上传→转写→有序片段→课件定位→失败→重试 ──────────────────────
async def a2(current, client, course_id, lecture_id):
    current["subject_id"] = SUBJECT_A

    # 1. 真实录音上传（真实 WAV 字节经 HTTP；媒体校验真实通过）
    wav_bytes = _read_material("newton-trial.wav") or make_wav(3.0)
    wav_filename = "newton-trial.wav" if _read_material("newton-trial.wav") else "tone.wav"
    fake = FakeASRProvider()
    set_provider(fake)

    r_audio = await client.post(
        f"/api/lectures/{lecture_id}/audio",
        files={"file": (wav_filename, wav_bytes, "audio/wav")},
    )
    audio_ok = r_audio.status_code == 201 and {"asset_id", "job_id"} <= set(r_audio.json())
    asset_id = r_audio.json().get("asset_id")
    job_id = r_audio.json().get("job_id")

    # 2. 真实课件上传（Markdown，多段 → 稳定段落定位）
    md_bytes = _read_material("newton-trial.md") or "# 例题\n质量2千克，加速度3米每二次方秒，合外力为6牛顿。\n".encode("utf-8")
    r_doc = await client.post(
        f"/api/courses/{course_id}/documents?lecture_id={lecture_id}",
        files={"file": ("newton-trial.md", md_bytes, "text/markdown")},
    )
    doc_ok = r_doc.status_code == 201 and r_doc.json().get("parse_status") == "parsed"
    doc_id = r_doc.json().get("id")
    doc_block_count = r_doc.json().get("block_count", 0)

    # 3. 派发转写任务（替身 ASR）→ 成功发布
    async with async_session_factory() as s:
        await recover_and_dispatch(s, provider=fake)
    async with async_session_factory() as s:
        job = await s.get(ProcessingJob, uuid.UUID(job_id))
        transcribe_ok = job.status == "succeeded" and job.output_version is not None

    # 4. 有序时间段 + 回听定位（转写段端点）
    r_segs = await client.get(f"/api/lectures/{lecture_id}/transcript")
    segs = r_segs.json()
    segs_ok = r_segs.status_code == 200 and len(segs) >= 3
    ordered = all(
        s["seq_index"] == i for i, s in enumerate(segs)
    )
    times = [s for s in segs if s["start_ms"] is not None and s["end_ms"] is not None]
    times_ok = len(times) == len(segs) and all(
        t["start_ms"] <= t["end_ms"] for t in times
    ) and all(
        times[i]["start_ms"] >= times[i - 1]["end_ms"] for i in range(1, len(times))
    )

    # 5. 课件原段定位（解析块 + 段落序号）
    r_blocks = await client.get(f"/api/documents/{doc_id}/blocks")
    blocks = r_blocks.json()
    blocks_ok = r_blocks.status_code == 200 and len(blocks) == doc_block_count
    located = all(b.get("paragraph_index") is not None for b in blocks)
    marker_found = any("牛顿第二定律" in b["text"] or "F = ma" in b["text"] or "合外力" in b["text"] for b in blocks)

    # 6. 模拟一次转写失败（替身 poll 失败 → 失败可见）
    fake.poll_result = "failure"
    r_audio2 = await client.post(
        f"/api/lectures/{lecture_id}/audio",
        files={"file": ("retry.wav", make_wav(5.0, freq=520.0), "audio/wav")},
    )
    job2_id = r_audio2.json().get("job_id")
    async with async_session_factory() as s:
        await run_job(s, uuid.UUID(job2_id), provider=fake)
    async with async_session_factory() as s:
        job2 = await s.get(ProcessingJob, uuid.UUID(job2_id))
    failure_visible = job2.status in ("failed", "retry_wait") and job2.error_type is not None

    # 7. 重试 → 有效结果（替身恢复成功）
    fake.poll_result = "success"
    r_retry = await client.post(f"/api/jobs/{job2_id}/retry")
    async with async_session_factory() as s:
        await run_job(s, uuid.UUID(job2_id), provider=fake)
    async with async_session_factory() as s:
        job2_after = await s.get(ProcessingJob, uuid.UUID(job2_id))
    retry_ok = r_retry.status_code == 200 and job2_after.status == "succeeded" and job2_after.output_version is not None

    record(
        "A2",
        "真实录音/课件上传→有序转写段→课件定位；模拟失败可见→重试得有效结果",
        audio_ok and doc_ok and transcribe_ok and segs_ok and ordered and times_ok
        and blocks_ok and located and marker_found and failure_visible and retry_ok,
        dict(
            audio=r_audio.status_code, asset_id=asset_id, job_id=job_id,
            doc=r_doc.status_code, doc_id=doc_id, parse_status=r_doc.json().get("parse_status"),
            block_count=doc_block_count, transcribe_status=job.status,
            segments=len(segs), ordered=ordered, times_ok=times_ok,
            seg_times=[(s["start_ms"], s["end_ms"]) for s in segs[:6]],
            blocks=len(blocks), located=located, marker_found=marker_found,
            block_sample=[(b["block_id"], b.get("paragraph_index"), b["text"][:20]) for b in blocks[:5]],
            failure_status=job2.status, failure_error=job2.error_type,
            retry_status=r_retry.status_code, retry_final=job2_after.status,
        ),
    )


async def main() -> None:
    await create_db(MAIN_DB)
    run_alembic("head", asyncpg_dsn(MAIN_DB))

    current = {"subject_id": SUBJECT_A}

    async def override_scope():
        return ScopeContext(subject_id=current["subject_id"])

    app.dependency_overrides[get_scope] = override_scope
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            try:
                course_id, l1_id, l2_id = await a1(current, client)
                await a2(current, client, course_id, l1_id)
            except Exception as e:  # noqa: BLE001
                import traceback
                tb = traceback.format_exc()
                (OUT / "_trace_a1a2.txt").write_text(tb, encoding="utf-8")
                record("脚本异常", "异常中断", False, {"error": repr(e), "trace": tb})
    finally:
        app.dependency_overrides.clear()

    failed = [r for r in RESULTS if r["result"] != "通过"]
    print("=" * 70)
    print(f"TOTAL {len(RESULTS)}  PASS {len(RESULTS) - len(failed)}  FAIL {len(failed)}")
    (OUT / "results.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "config.json").write_text(
        json.dumps(
            {
                "asr_mode": settings.ASR_MODE,
                "subject_a": SUBJECT_A,
                "subject_b": SUBJECT_B,
                "note": "模拟路径：FakeASRProvider 可控替身 + 进程内 HTTP + 跨用户 subject 注入",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    await drop_db(MAIN_DB)
    upload_parent = Path(settings.UPLOAD_DIR).parent
    if upload_parent.exists():
        shutil.rmtree(upload_parent, ignore_errors=True)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
