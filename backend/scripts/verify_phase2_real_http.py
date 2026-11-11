"""阶段 2「课程、课时与资料输入」真实服务路径验证。

前置：真实 backend（uvicorn app.main:app，127.0.0.1:8787）已启动，连活跃库
yuan_classroom_local（ASR_MODE=real、CLASSROOM_PAID_CALLS_ENABLED=true、OSS+DashScope 已配置）。

本脚本做「真实 HTTP」上传（课程/课时/真实录音/真实课件），并仅派发 ASR 任务
（recover_and_dispatch + 真实 DashScope provider，不触发 organize/index），
随后经真实 HTTP 读取转写段与课件块。真实供应商（DashScope/OSS）结果为「真实服务结果」。

运行（backend 目录，需先启动 uvicorn）：
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  python scripts/verify_phase2_real_http.py
"""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

from app.config import settings
from app.services import rate_limit_service
from app.services.asr_provider import get_provider
from app.services.job_service import recover_and_dispatch

BASE_URL = "http://127.0.0.1:8787"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent / "docs" / "classroom-agent" / "reset-v1" / "evidence" / "phase2"
OUT.mkdir(parents=True, exist_ok=True)
MATERIALS = ROOT.parent / "docs" / "classroom-agent" / "trial-materials"

RESULTS: list[dict] = []


def record(name: str, ok: bool, detail) -> None:
    row = {"id": name, "result": "通过" if ok else "不通过", "detail": detail}
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


async def main() -> None:
    provider = get_provider()
    print(f"provider={provider.name} asr_mode={settings.ASR_MODE} "
          f"paid_calls={settings.CLASSROOM_PAID_CALLS_ENABLED} db={settings.DATABASE_URL.rsplit('/',1)[-1]}")

    await rate_limit_service.init_pool()
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
            # 1. 创建课程 + 两个课时（真实 HTTP）
            r_course = await client.post("/api/courses", json={
                "title": f"真实链路验收课程-{uuid.uuid4().hex[:6]}",
                "teacher": "张老师", "term": "2026秋",
            })
            course_ok = r_course.status_code == 201
            course_id = r_course.json().get("id")
            course_title = r_course.json().get("title")

            r_l1 = await client.post(f"/api/courses/{course_id}/lectures", json={"title": "第 1 讲 牛顿第二定律"})
            r_l2 = await client.post(f"/api/courses/{course_id}/lectures", json={"title": "第 2 讲 例题精讲"})
            l1_id = r_l1.json().get("id")
            l2_id = r_l2.json().get("id")
            lecture_ok = course_ok and r_l1.status_code == 201 and r_l2.status_code == 201

            r_list = await client.get("/api/courses")
            mine = next((c for c in r_list.json().get("items", []) if c["id"] == course_id), None)
            count_ok = mine is not None and mine.get("lecture_count") == 2

            # 2. 上传真实录音 + 真实课件
            wav_bytes = (MATERIALS / "newton-trial.wav").read_bytes()
            md_bytes = (MATERIALS / "newton-trial.md").read_bytes()
            r_audio = await client.post(
                f"/api/lectures/{l1_id}/audio",
                files={"file": ("newton-trial.wav", wav_bytes, "audio/wav")},
            )
            audio_ok = r_audio.status_code == 201
            job_id = r_audio.json().get("job_id")
            asset_id = r_audio.json().get("asset_id")

            r_doc = await client.post(
                f"/api/courses/{course_id}/documents?lecture_id={l1_id}",
                files={"file": ("newton-trial.md", md_bytes, "text/markdown")},
            )
            doc_ok = r_doc.status_code == 201 and r_doc.json().get("parse_status") == "parsed"
            doc_id = r_doc.json().get("id")
            doc_block_count = r_doc.json().get("block_count", 0)

            # 3. 派发 ASR（真实 DashScope provider；不触发 organize/index）
            from app.db.session import async_session_factory
            async with async_session_factory() as db:
                stats = await recover_and_dispatch(db, provider=provider)

            # 4. 读取任务终态
            from app.models.processing_job import ProcessingJob
            from sqlalchemy import select
            async with async_session_factory() as db:
                job = await db.get(ProcessingJob, uuid.UUID(job_id))
                job_status = job.status
                job_error = job.error_type
                output_version = str(job.output_version) if job.output_version else None

            # 5. 真实转写段 + 课件块
            r_segs = await client.get(f"/api/lectures/{l1_id}/transcript")
            segs = r_segs.json() if r_segs.status_code == 200 else []
            r_blocks = await client.get(f"/api/documents/{doc_id}/blocks")
            blocks = r_blocks.json() if r_blocks.status_code == 200 else []

            seg_ok = r_segs.status_code == 200 and len(segs) > 0
            ordered = all(s.get("seq_index") == i for i, s in enumerate(segs)) and len(segs) > 0
            times_ok = all(s.get("start_ms") is not None and s.get("end_ms") is not None
                           and s["start_ms"] <= s["end_ms"] for s in segs)

            record(
                "真实-创建与上传",
                lecture_ok and count_ok and audio_ok and doc_ok,
                dict(course=course_title, lecture1=l1_id, lecture2=l2_id,
                     lecture_count=mine.get("lecture_count") if mine else None,
                     audio=r_audio.status_code, asset_id=asset_id, job_id=job_id,
                     doc=r_doc.status_code, doc_id=doc_id, block_count=doc_block_count),
            )
            record(
                "真实-ASR与定位",
                job_status == "succeeded" and seg_ok and ordered and times_ok
                and len(blocks) == doc_block_count,
                dict(asr_stats=stats, job_status=job_status, job_error=job_error,
                     output_version=output_version, segments=len(segs), ordered=ordered,
                     times_ok=times_ok,
                     seg_sample=[(s.get("seq_index"), s.get("start_ms"), s.get("end_ms"),
                                  (s.get("content") or "")[:30]) for s in segs[:8]],
                     blocks=len(blocks),
                     block_sample=[(b.get("block_id"), b.get("paragraph_index"), b.get("text")[:30])
                                   for b in blocks[:6]]),
            )
    finally:
        await rate_limit_service.close_pool()

    failed = [r for r in RESULTS if r["result"] != "通过"]
    print("=" * 70)
    print(f"TOTAL {len(RESULTS)}  PASS {len(RESULTS) - len(failed)}  FAIL {len(failed)}")
    (OUT / "real_results.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
