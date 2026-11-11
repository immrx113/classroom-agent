"""Phase 03「全课整理」冻结验收：P03-01～P03-16（P0）独立验证。

针对 docs/classroom-agent/acceptance/phase-03-frozen-checklist-v1.md。
P1（P03-17/18/19）转人工，不在本脚本自动验证。

运行（backend 目录）：
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  python scripts/verify_phase3_frozen.py
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.update(
    OPENAI_API_KEY="", DASHSCOPE_API_KEY="", PYTHONPATH=".", PYTHONUTF8="1"
)

import asyncpg
from app.config import settings

BASE = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
PREFIX = "yuan_phase3_test_" + uuid.uuid4().hex[:10] + "_"
MAIN_DB = PREFIX + "main"

# 在导入会创建引擎的模块前，把引擎指向隔离测试库，绝不触碰生产库 yuan_meet。
settings.DATABASE_URL = BASE.replace("postgresql://", "postgresql+asyncpg://") + "/" + MAIN_DB
os.environ["DATABASE_URL"] = settings.DATABASE_URL

from app.api.deps import get_scope
from app.db.session import async_session_factory
from app.main import app
from app.models.audio_asset import AudioAsset
from app.models.course import Course
from app.models.evidence import ArtifactEvidence, EvidenceSpan
from app.models.meeting import Meeting
from app.models.note_version import NoteVersion
from app.models.organize import ChunkManifest, ChunkItem, IndexIntent
from app.models.processing_job import ProcessingJob, OutboxEvent
from app.models.transcript_version import TranscriptSegment, TranscriptVersion
import app.services.organize_service as org_module
from app.services.organize_model import (
    FakeOrganizeModel,
    ModelError,
    OrganizeModel,
    WorkerCrash,
)
from app.services.organize_service import organize_service
from app.services.scope import ScopeContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]  # backend/（alembic 运行目录）
OUT = ROOT.parent / "docs" / "classroom-agent" / "acceptance" / "evidence-phase3-20261029"
OUT.mkdir(parents=True, exist_ok=True)

SUBJECT_A = "subject-a"
SUBJECT_B = "subject-b"
DBS: list[str] = [MAIN_DB]
RESULTS: list[dict] = []


def record(pid: str, check: str, ok: bool, detail) -> None:
    row = dict(id=pid, check=check, result="通过" if ok else "不通过", detail=detail)
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


# 源码快照：绑定本轮实现、迁移、脚本与冻结清单（证据口径，不记录密钥/正文）
SOURCE_FILES = [
    "app/services/organize_service.py",
    "app/services/organize_model.py",
    "app/services/job_service.py",
    "app/api/notes.py",
    "app/schemas/note.py",
    "app/models/organize.py",
    "app/models/note_version.py",
    "alembic/versions/2026_10_29_0001-b3c4d5e6f7a8_add_phase3_organize.py",
    "scripts/verify_phase3_frozen.py",
    "../docs/classroom-agent/acceptance/phase-03-frozen-checklist-v1.md",
    # P03-14 最小笔记阅读与引用回听前端源码（路由/页面/回听逻辑）
    "../frontend/src/features/notes/pages/note-detail-page.tsx",
    "../frontend/src/features/notes/api.ts",
    "../frontend/src/features/notes/hooks/use-notes.ts",
    "../frontend/src/router/routes.tsx",
    "../frontend/src/features/meetings/pages/meeting-detail-page.tsx",
    "../frontend/src/types/index.ts",
    "../frontend/src/lib/constants.ts",
]


def sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def pg_dsn(name: str) -> str:
    return BASE + "/" + name


def asyncpg_dsn(name: str) -> str:
    return BASE.replace("postgresql://", "postgresql+asyncpg://") + "/" + name


def now() -> datetime:
    return datetime.now(timezone.utc)


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
        raise RuntimeError(
            f"alembic upgrade {revision} failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        )


async def create_lecture(owner_id: str, title: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with async_session_factory() as db:
        course = Course(owner_id=owner_id, title=f"course-{title}")
        db.add(course)
        await db.flush()
        meeting = Meeting(course_id=course.id, title=title, status="pending")
        db.add(meeting)
        await db.flush()
        mid, cid = meeting.id, course.id
        await db.commit()
        return mid, cid


async def set_archived(course_id: uuid.UUID, archived: bool) -> None:
    async with async_session_factory() as db:
        c = await db.get(Course, course_id)
        c.archived_at = now() if archived else None
        await db.commit()


def _seg(seq, content, speaker="t", start_ms=None, end_ms=None) -> dict:
    return dict(seq_index=seq, speaker=speaker, content=content,
                start_ms=start_ms, end_ms=end_ms)


def _pad(text: str, n: int) -> str:
    return text + "。" * (n - len(text))


async def add_transcript(
    lecture_id: uuid.UUID, segments: list[dict], *, is_demo: bool = False, active: bool = True
) -> tuple[uuid.UUID, uuid.UUID]:
    """直接落一份转写版本 + 片段；active=True 时切换课时当前指针并撤销旧有效版本。"""
    async with async_session_factory() as db:
        meeting = await db.get(Meeting, lecture_id)
        asset = AudioAsset(
            lecture_id=lecture_id, storage_key="assets/test.wav",
            duration_ms=max((s.get("end_ms") or 0) for s in segments) or None,
            is_demo=is_demo,
        )
        db.add(asset)
        await db.flush()
        version = TranscriptVersion(
            audio_asset_id=asset.id, lecture_id=lecture_id, version=1,
            provider="fake", status="building",
        )
        db.add(version)
        await db.flush()
        for s in segments:
            db.add(
                TranscriptSegment(
                    version_id=version.id, seq_index=s["seq_index"],
                    speaker=s.get("speaker"), content=s["content"],
                    start_ms=s.get("start_ms"), end_ms=s.get("end_ms"),
                )
            )
        await db.flush()
        if active:
            if meeting.active_transcript_version_id is not None:
                old = await db.get(TranscriptVersion, meeting.active_transcript_version_id)
                if old is not None and old.id != version.id:
                    old.status = "superseded"
            meeting.active_audio_id = asset.id
            meeting.active_transcript_version_id = version.id
            meeting.status = "processed"
            version.status = "active"
        else:
            version.status = "ready"
        await db.commit()
        return asset.id, version.id


async def make_organize_job(lecture_id, version_id, subject=SUBJECT_A) -> uuid.UUID:
    async with async_session_factory() as db:
        job = await organize_service.create_organize_job(
            db, ScopeContext(subject_id=subject),
            lecture_id=lecture_id, transcript_version_id=version_id,
        )
        return job.id


async def run_organize_job(job_id, fake) -> None:
    async with async_session_factory() as db:
        await organize_service.run_organize_job(db, job_id, model=fake)


async def organize_full(lecture_id, version_id, fake, subject=SUBJECT_A) -> uuid.UUID:
    job_id = await make_organize_job(lecture_id, version_id, subject)
    await run_organize_job(job_id, fake)
    return job_id


async def fetch_job(job_id) -> ProcessingJob | None:
    async with async_session_factory() as db:
        return await db.get(ProcessingJob, job_id)


async def fetch_note(note_id) -> NoteVersion | None:
    async with async_session_factory() as db:
        return await db.get(NoteVersion, note_id)


def make_long_segments(n=20, base=600) -> list[dict]:
    segs = []
    for i in range(n):
        content = f"第{i}段：" + "字" * (base - 4)
        segs.append(_seg(i, content, start_ms=i * 5000, end_ms=(i + 1) * 5000 - 100))
    return segs


async def main() -> None:
    started = datetime.now(timezone.utc)
    await create_db(MAIN_DB)
    run_alembic("head", asyncpg_dsn(MAIN_DB))

    current = {"subject_id": SUBJECT_A, "scope": None}

    async def override_scope():
        if current["scope"] is not None:
            return current["scope"]
        return ScopeContext(subject_id=current["subject_id"])

    app.dependency_overrides[get_scope] = override_scope
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for pid, coro in tests(current, client):
                try:
                    await coro
                except Exception as e:  # noqa: BLE001
                    import traceback
                    tb = traceback.format_exc()
                    (OUT / f"_trace_{pid}.txt").write_text(tb, encoding="utf-8")
                    record(pid, "脚本异常", False, {"error": repr(e), "trace": tb})
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
                "context_budget": settings.ORGANIZE_CONTEXT_BUDGET,
                "prompt_reserve": settings.ORGANIZE_PROMPT_RESERVE,
                "output_reserve": settings.ORGANIZE_OUTPUT_RESERVE,
                "input_budget": settings.ORGANIZE_CONTEXT_BUDGET - settings.ORGANIZE_PROMPT_RESERVE - settings.ORGANIZE_OUTPUT_RESERVE,
                "overlap": settings.ORGANIZE_CHUNK_OVERLAP,
                "max_attempts": settings.ORGANIZE_MAX_ATTEMPTS,
                "organize_model": settings.ORGANIZE_MODEL,
                "job_heartbeat_timeout": settings.JOB_HEARTBEAT_TIMEOUT_SECONDS,
                "database": MAIN_DB,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    finished = datetime.now(timezone.utc)
    (OUT / "run.json").write_text(
        json.dumps(
            {
                "exit_code": 1 if failed else 0,
                "started_utc": started.isoformat(),
                "finished_utc": finished.isoformat(),
                "databases": [asyncpg_dsn(n) for n in DBS],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest = {}
    for rel in SOURCE_FILES:
        p = ROOT / rel if not rel.startswith("../") else (ROOT.parent / rel[3:])
        manifest[rel] = sha256(p) if p.exists() else None
    (OUT / "source-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for name in DBS:
        await drop_db(name)
    if failed:
        sys.exit(1)


def tests(current, client):
    return [
        ("P03-01", p03_01(current, client)),
        ("P03-02", p03_02(current, client)),
        ("P03-03", p03_03(current, client)),
        ("P03-04", p03_04(current, client)),
        ("P03-05", p03_05(current, client)),
        ("P03-06", p03_06(current, client)),
        ("P03-07", p03_07(current, client)),
        ("P03-08", p03_08(current, client)),
        ("P03-09", p03_09(current, client)),
        ("P03-10", p03_10(current, client)),
        ("P03-11", p03_11(current, client)),
        ("P03-12", p03_12(current, client)),
        ("P03-13", p03_13(current, client)),
        ("P03-14", p03_14(current, client)),
        ("P03-15", p03_15(current, client)),
        ("P03-16", p03_16()),
    ]


# ── P03-01 权限与范围 ────────────────────────────────────────────────
async def p03_01(current, client):
    current["scope"] = None
    a1, a1_course = await create_lecture(SUBJECT_A, "p0301-a")
    l2, l2_course = await create_lecture(SUBJECT_A, "p0301-l2")
    _, v1 = await add_transcript(a1, [_seg(0, "内容A", start_ms=0, end_ms=1000)])
    await add_transcript(l2, [_seg(0, "内容L2", start_ms=0, end_ms=1000)])

    # A 发起整理并运行出笔记
    current["subject_id"] = SUBJECT_A
    current["scope"] = None
    r = await client.post(f"/api/lectures/{a1}/organize", json={"transcript_version_id": str(v1)})
    ok_create = r.status_code == 201
    job_id = uuid.UUID(r.json()["id"])
    fake = FakeOrganizeModel()
    await run_organize_job(job_id, fake)
    note_id = (await fetch_job(job_id)).output_version

    # A 读取正常
    r_notes = await client.get(f"/api/lectures/{a1}/notes")
    r_note = await client.get(f"/api/notes/{note_id}")

    # B 对他主资源：发起/查询/读取/编辑/发布均拒绝
    current["scope"] = ScopeContext(subject_id=SUBJECT_B)
    r_b_create = await client.post(f"/api/lectures/{a1}/organize", json={"transcript_version_id": str(v1)})
    r_b_job = await client.get(f"/api/jobs/{job_id}")
    r_b_notes = await client.get(f"/api/lectures/{a1}/notes")
    r_b_note = await client.get(f"/api/notes/{note_id}")
    r_b_edit = await client.patch(f"/api/notes/{note_id}", json={"title": "篡改"})
    r_b_pub = await client.post(f"/api/notes/{note_id}/publish")

    # A 用 L2 范围访问 L1 → 拒绝
    current["scope"] = ScopeContext(subject_id=SUBJECT_A, lecture_id=l2)
    r_scope = await client.get(f"/api/lectures/{a1}/notes")

    # 归档 C1 后 A 普通入口拒绝
    current["scope"] = None
    await set_archived(a1_course, True)
    r_arch = await client.get(f"/api/lectures/{a1}/notes")

    async with async_session_factory() as db:
        jobs = (await db.execute(
            select(func.count(ProcessingJob.id)).where(
                ProcessingJob.resource_id == a1, ProcessingJob.type == "organize"
            )
        )).scalar_one()

    record(
        "P03-01",
        "整理/笔记/编辑/发布继承权限与范围 + 被拒写入无副作用",
        ok_create and r_notes.status_code == 200 and r_note.status_code == 200
        and r_b_create.status_code == 404 and r_b_job.status_code == 404
        and r_b_notes.status_code == 404 and r_b_note.status_code == 404
        and r_b_edit.status_code == 404 and r_b_pub.status_code == 404
        and r_scope.status_code == 404 and r_arch.status_code == 404 and jobs == 1,
        dict(a_create=r.status_code, a_notes=r_notes.status_code, a_note=r_note.status_code,
             b_create=r_b_create.status_code, b_job=r_b_job.status_code,
             b_notes=r_b_notes.status_code, b_note=r_b_note.status_code,
             b_edit=r_b_edit.status_code, b_pub=r_b_pub.status_code,
             scope_mismatch=r_scope.status_code, archived=r_arch.status_code, jobs=jobs),
    )


# ── P03-02 固定输入版本与配置 ────────────────────────────────────────
async def p03_02(current, client):
    current["scope"] = None
    a1, _ = await create_lecture(SUBJECT_A, "p0302-a")
    l2, _ = await create_lecture(SUBJECT_A, "p0302-l2")
    _, v1 = await add_transcript(a1, [_seg(0, "V1内容", start_ms=0, end_ms=1000)])
    _, v_l2 = await add_transcript(l2, [_seg(0, "L2版本", start_ms=0, end_ms=1000)])
    _, demo_vid = await add_transcript(a1, [_seg(0, "演示内容", start_ms=0, end_ms=1000)], is_demo=True, active=False)

    current["subject_id"] = SUBJECT_A
    r = await client.post(f"/api/lectures/{a1}/organize", json={"transcript_version_id": str(v1)})
    job_id = uuid.UUID(r.json()["id"])
    r_missing = await client.post(f"/api/lectures/{a1}/organize", json={"transcript_version_id": str(uuid.uuid4())})
    r_foreign = await client.post(f"/api/lectures/{a1}/organize", json={"transcript_version_id": str(v_l2)})
    r_demo = await client.post(f"/api/lectures/{a1}/organize", json={"transcript_version_id": str(demo_vid)})

    # 运行中把当前转写切为 V2 → 任务仍绑定 V1，结果归属 V1 且不发布
    await add_transcript(a1, [_seg(0, "V2内容", start_ms=0, end_ms=1000)])
    job = await fetch_job(job_id)
    bound_v1 = job.input_version == v1
    config_ok = bool(job.config_hash)

    fake = FakeOrganizeModel()
    await run_organize_job(job_id, fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    note_bound_v1 = note.transcript_version_id == v1
    note_draft = note.status == "draft"

    record(
        "P03-02",
        "任务固定绑定可信输入 V1 + 配置摘要 + 非法/演示来源拒绝",
        r.status_code == 201 and r_missing.status_code == 404 and r_foreign.status_code == 404
        and r_demo.status_code == 422 and bound_v1 and config_ok and note_bound_v1 and note_draft,
        dict(create=r.status_code, missing=r_missing.status_code, foreign=r_foreign.status_code,
             demo=r_demo.status_code, bound_v1=bound_v1, config_hash=bool(job.config_hash),
             note_bound_v1=note_bound_v1, note_status=note.status),
    )


# ── P03-03 分块清单全量覆盖 + 预算 ───────────────────────────────────
async def p03_03(current, client):
    current["scope"] = None
    a1, _ = await create_lecture(SUBJECT_A, "p0303-a")
    segs = make_long_segments(n=20, base=600)
    _, v1 = await add_transcript(a1, segs)

    async with async_session_factory() as db:
        job = await organize_service.create_organize_job(
            db, ScopeContext(subject_id=SUBJECT_A), lecture_id=a1, transcript_version_id=v1
        )
        manifest = await organize_service.build_manifest(db, job)
    async with async_session_factory() as db:
        items = (await db.execute(
            select(ChunkItem).where(ChunkItem.manifest_id == manifest.id).order_by(ChunkItem.chunk_index)
        )).scalars().all()

    input_budget = settings.ORGANIZE_CONTEXT_BUDGET - settings.ORGANIZE_PROMPT_RESERVE - settings.ORGANIZE_OUTPUT_RESERVE
    seq_to_len = {s["seq_index"]: len(s["content"]) for s in segs}
    all_seq = set(seq_to_len)
    core_union: set[int] = set()
    for it in items:
        core_union |= set(it.core_segments or [])
    full_coverage = core_union == all_seq == set(manifest.input_segment_ids)
    tail_covered = max(all_seq) in core_union
    each_within = all(
        sum(seq_to_len[i] for i in (it.core_segments or [])) <= input_budget for it in items
    )
    # 实际输入预算须覆盖「核心 + 相邻重叠上下文」（P03-03 修正：不能只断言核心长度）
    actual_within = all(
        sum(seq_to_len[i] for i in (it.core_segments or []))
        + sum(seq_to_len[i] for i in (it.context_segments or [])) <= input_budget
        for it in items
    )
    n_chunks = len(items) >= 3
    total_len = sum(seq_to_len.values())
    over_8000 = total_len > 8000

    overlap = settings.ORGANIZE_CHUNK_OVERLAP
    overlap_ok = True
    for i in range(1, len(items)):
        prev_core = items[i - 1].core_segments or []
        expected = prev_core[-overlap:] if overlap > 0 else []
        if set(expected) - set(items[i].context_segments or []):
            overlap_ok = False
    # 上下文不冒充新增核心覆盖：核心并集严格等于输入，上下文单独记录
    context_not_core = all(
        not (set(it.context_segments or []) & set(it.core_segments or [])) for it in items
    )

    record(
        "P03-03",
        "核心覆盖全量含末尾 + 核心与上下文均在输入预算内 + 重叠上下文不冒充核心",
        full_coverage and tail_covered and each_within and actual_within and n_chunks and over_8000
        and overlap_ok and context_not_core,
        dict(n_chunks=len(items), total_len=total_len, input_budget=input_budget,
             core_union_size=len(core_union), full_coverage=full_coverage,
             each_within=each_within, actual_within=actual_within, overlap_ok=overlap_ok),
    )


# ── P03-04 首中尾事实 + 末尾锚点 ─────────────────────────────────────
async def p03_04(current, client):
    current["scope"] = None
    a1, _ = await create_lecture(SUBJECT_A, "p0304-a")
    segs = make_long_segments(n=20, base=600)
    head_text = "开头事实：定义与结论"
    mid_text = "中部事实：中间推导"
    tail_text = "末尾事实：末尾可回听锚点"
    segs[0]["content"] = head_text
    segs[10]["content"] = mid_text
    segs[19]["content"] = tail_text
    _, v1 = await add_transcript(a1, segs)

    fake = FakeOrganizeModel()
    job_id = await organize_full(a1, v1, fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    kcs = note.content["key_conclusions"]
    texts = {c["text"] for c in kcs}
    tail_entry = next((c for c in kcs if c["text"] == tail_text), None)

    tail_has_evidence = bool(tail_entry and tail_entry["evidence_ids"])
    tail_ev_ok = False
    if tail_entry and tail_entry["evidence_ids"]:
        async with async_session_factory() as db:
            ev = await db.get(EvidenceSpan, uuid.UUID(tail_entry["evidence_ids"][0]))
            tail_ev_ok = ev.segment_start == 19 and ev.quote == tail_text

    called = sorted(c["chunk_index"] for c in fake.calls)
    all_chunks = called == list(range(len(called)))

    record(
        "P03-04",
        "首中尾事实进入笔记 + 末尾事实有可核对证据 + 替身按块调用无预置答案",
        head_text in texts and mid_text in texts and tail_text in texts
        and tail_has_evidence and tail_ev_ok and all_chunks and len(called) >= 3,
        dict(has_head=head_text in texts, has_mid=mid_text in texts, has_tail=tail_text in texts,
             tail_has_evidence=tail_has_evidence, tail_ev_ok=tail_ev_ok,
             chunks_called=called),
    )


# ── P03-05 跨块不截断 ────────────────────────────────────────────────
async def p03_05(current, client):
    current["scope"] = None
    a1, _ = await create_lecture(SUBJECT_A, "p0305-a")
    # 8 个均匀 600 字片段，预算收缩后核心 4 段一块：cond 在第 0 块末尾、concl 在第 1 块开头
    cond_text = _pad("问题/条件：已知 A 求 B", 600)
    concl_text = _pad("结论/推导：因此 B 成立", 600)
    segs = [_seg(i, "填" * 600, start_ms=i * 5000, end_ms=(i + 1) * 5000 - 100) for i in range(8)]
    segs[3]["content"] = cond_text
    segs[4]["content"] = concl_text
    _, v1 = await add_transcript(a1, segs)

    fake = FakeOrganizeModel()
    job_id = await organize_full(a1, v1, fake)
    async with async_session_factory() as db:
        manifest = (await db.execute(
            select(ChunkManifest).where(ChunkManifest.job_id == job_id)
        )).scalars().first()
        items = (await db.execute(
            select(ChunkItem).where(ChunkItem.manifest_id == manifest.id).order_by(ChunkItem.chunk_index)
        )).scalars().all()
        cond_chunk = next(i.chunk_index for i in items if 3 in (i.core_segments or []))
        concl_chunk = next(i.chunk_index for i in items if 4 in (i.core_segments or []))
        note = await db.get(NoteVersion, (await db.get(ProcessingJob, job_id)).output_version)
        kcs = note.content["key_conclusions"]
        cond_entry = next((c for c in kcs if c["text"] == cond_text), None)
        concl_entry = next((c for c in kcs if c["text"] == concl_text), None)

    record(
        "P03-05",
        "跨块条件与结论均保留并可回到两侧来源",
        cond_chunk != concl_chunk and cond_entry is not None and concl_entry is not None
        and bool(cond_entry["evidence_ids"]) and bool(concl_entry["evidence_ids"]),
        dict(cond_chunk=cond_chunk, concl_chunk=concl_chunk,
             cond_has_ev=bool(cond_entry and cond_entry["evidence_ids"]),
             concl_has_ev=bool(concl_entry and concl_entry["evidence_ids"])),
    )


# ── P03-06 重叠去重 + 不同条件不误合并 ───────────────────────────────
async def p03_06(current, client):
    current["scope"] = None
    a1, _ = await create_lecture(SUBJECT_A, "p0306-a")
    dup_text = "同一个事实被重复陈述"
    cond_a = "关于主题X，条件A成立"
    cond_b = "关于主题X，条件B成立"
    segs = [
        _seg(0, dup_text, start_ms=0, end_ms=1000),
        _seg(1, cond_a, start_ms=2000, end_ms=3000),
        _seg(2, dup_text, start_ms=4000, end_ms=5000),
        _seg(3, cond_b, start_ms=6000, end_ms=7000),
    ]
    _, v1 = await add_transcript(a1, segs)
    fake = FakeOrganizeModel()
    job_id = await organize_full(a1, v1, fake)
    note = await fetch_note((await fetch_job(job_id)).output_version)
    kcs = note.content["key_conclusions"]
    dup_count = sum(1 for c in kcs if c["text"] == dup_text)
    has_a = any(c["text"] == cond_a for c in kcs)
    has_b = any(c["text"] == cond_b for c in kcs)

    record(
        "P03-06",
        "同文重复去重为一条 + 不同限定条件均保留",
        dup_count == 1 and has_a and has_b,
        dict(dup_count=dup_count, has_cond_a=has_a, has_cond_b=has_b),
    )


# ── P03-07 结构可读 + 非法结果不伪成功 ───────────────────────────────
async def p03_07(current, client):
    current["scope"] = None
    # 有效样本
    a1, _ = await create_lecture(SUBJECT_A, "p0307-a")
    _, v1 = await add_transcript(a1, [_seg(0, "有效正文内容", start_ms=0, end_ms=1000)])
    job_id = await organize_full(a1, v1, FakeOrganizeModel())
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    valid = (
        job.status == "succeeded" and note.status == "published"
        and isinstance(note.content.get("title"), str)
        and isinstance(note.content.get("outline"), list)
        and isinstance(note.content.get("sections"), list)
        and isinstance(note.content.get("key_conclusions"), list)
        and note.content["source_version"]["transcript_version_id"] == str(v1)
    )

    # 缺字段块 → 明确失败，不发布
    a2, _ = await create_lecture(SUBJECT_A, "p0307-b")
    _, v2 = await add_transcript(a2, [_seg(0, "坏块正文", start_ms=0, end_ms=1000)])
    fake2 = FakeOrganizeModel()
    fake2.invalid_schema_chunks = {0}
    job2_id = await organize_full(a2, v2, fake2)
    job2 = await fetch_job(job2_id)
    invalid_failed = job2.status == "failed"
    note2 = await fetch_note(job2.output_version) if job2.output_version else None
    invalid_not_published = note2 is None or note2.status != "published"

    # 类型错误（最终汇总 outline 非列表）→ 明确失败
    a3, _ = await create_lecture(SUBJECT_A, "p0307-c")
    _, v3 = await add_transcript(a3, [_seg(0, "汇总类型错误", start_ms=0, end_ms=1000)])
    fake3 = FakeOrganizeModel()
    fake3.summarize_invalid = True
    job3_id = await organize_full(a3, v3, fake3)
    job3 = await fetch_job(job3_id)
    type_failed = job3.status == "failed" and job3.output_version is None

    # 最终汇总失败 → 明确失败
    a4, _ = await create_lecture(SUBJECT_A, "p0307-d")
    _, v4 = await add_transcript(a4, [_seg(0, "汇总失败正文", start_ms=0, end_ms=1000)])
    fake4 = FakeOrganizeModel()
    fake4.summarize_error = True
    job4_id = await organize_full(a4, v4, fake4)
    job4 = await fetch_job(job4_id)
    sum_failed = job4.status == "failed" and job4.output_version is None

    record(
        "P03-07",
        "有效笔记具备完整结构 + 缺字段/类型错误/汇总失败均明确失败不伪成功",
        valid and invalid_failed and invalid_not_published and type_failed and sum_failed,
        dict(valid=valid, invalid_status=job2.status, invalid_note=note2.status if note2 else None,
             type_status=job3.status, type_has_note=job3.output_version is not None,
             sum_status=job4.status, sum_has_note=job4.output_version is not None),
    )


# ── P03-08 教师任务 ─────────────────────────────────────────────────
async def p03_08(current, client):
    current["scope"] = None
    # 有任务
    a1, _ = await create_lecture(SUBJECT_A, "p0308-a")
    _, v1 = await add_transcript(a1, [
        _seg(0, "课堂正文一", start_ms=0, end_ms=1000),
        _seg(1, "【作业】下次课前完成第三章习题", start_ms=2000, end_ms=3000),
    ])
    job_id = await organize_full(a1, v1, FakeOrganizeModel())
    note = await fetch_note((await fetch_job(job_id)).output_version)
    tasks = note.content["tasks"]
    has_task = len(tasks) == 1
    task = tasks[0] if tasks else {}
    task_ok = (
        task.get("task") == "下次课前完成第三章习题"
        and task.get("due_text") == "下次课前完成第三章习题"
        and task.get("due_at") is None
        and task.get("task_type") == "teacher_assignment"
        and bool(task.get("evidence_ids"))
    )

    # 无任务成功
    a2, _ = await create_lecture(SUBJECT_A, "p0308-b")
    _, v2 = await add_transcript(a2, [_seg(0, "没有作业的课堂", start_ms=0, end_ms=1000)])
    job2_id = await organize_full(a2, v2, FakeOrganizeModel())
    job2 = await fetch_job(job2_id)
    note2 = await fetch_note(job2.output_version)
    no_task_succeeded = job2.status == "succeeded" and note2.content["tasks"] == []

    # 任务抽取失败 → 明确失败，不伪装无作业
    a3, _ = await create_lecture(SUBJECT_A, "p0308-c")
    _, v3 = await add_transcript(a3, [_seg(0, "有作业但抽取失败", start_ms=0, end_ms=1000)])
    fake3 = FakeOrganizeModel()
    fake3.task_error = True
    job3_id = await organize_full(a3, v3, fake3)
    job3 = await fetch_job(job3_id)
    task_error_failed = job3.status == "failed"

    record(
        "P03-08",
        "教师任务保留原文/evidence + 无作业可成功 + 抽取失败不伪装无作业",
        has_task and task_ok and no_task_succeeded and task_error_failed,
        dict(has_task=has_task, task=task, no_task_status=job2.status,
             no_task_tasks=note2.content["tasks"] if note2 else None,
             task_error_status=job3.status),
    )


# ── P03-09 服务端解析证据 + 时间不可伪造 ─────────────────────────────
class CraftedModel(OrganizeModel):
    name = "crafted"

    def __init__(self, conclusions, tasks=None):
        self.conclusions = conclusions
        self.tasks = tasks or []
        self.calls: list[int] = []

    async def extract_chunk(self, chunk):
        self.calls.append(chunk.chunk_index)
        if chunk.chunk_index == 0:
            return {"conclusions": self.conclusions, "tasks": self.tasks}
        return {"conclusions": [], "tasks": []}

    async def summarize(self, conclusions, tasks, meta):
        sections = [{"heading": "要点", "points": list(conclusions)}]
        return {
            "title": meta.get("title", "课堂笔记"),
            "outline": [s["heading"] for s in sections],
            "sections": sections,
            "key_conclusions": list(conclusions),
            "tasks": list(tasks),
        }


async def p03_09(current, client):
    current["scope"] = None
    # 时间取自可信来源 + 缺时间不伪造
    a1, _ = await create_lecture(SUBJECT_A, "p0309-a")
    _, v1 = await add_transcript(a1, [
        _seg(0, "合法引用正文", start_ms=1000, end_ms=2000),
        _seg(1, "缺时间片段", start_ms=None, end_ms=None),
    ])
    job_id = await organize_full(a1, v1, FakeOrganizeModel())
    note = await fetch_note((await fetch_job(job_id)).output_version)
    kcs = note.content["key_conclusions"]
    kc0 = next(c for c in kcs if c["text"] == "合法引用正文")
    kc1 = next(c for c in kcs if c["text"] == "缺时间片段")
    async with async_session_factory() as db:
        ev0 = await db.get(EvidenceSpan, uuid.UUID(kc0["evidence_ids"][0]))
        ev1 = await db.get(EvidenceSpan, uuid.UUID(kc1["evidence_ids"][0]))
        time_from_seg = ev0.start_ms == 1000 and ev0.end_ms == 2000
        no_time_ok = ev1.start_ms is None and ev1.end_ms is None and ev1.segment_start == 1 and ev1.quote == "缺时间片段"

    # 非法引用：虚构 evidence_id / 模型时间 / 越范围片段 / 不匹配 quote
    a2, _ = await create_lecture(SUBJECT_A, "p0309-b")
    _, v2 = await add_transcript(a2, [_seg(0, "真实片段", start_ms=0, end_ms=1000)])
    crafted = CraftedModel([
        {"text": "合法结论", "quote": "真实片段", "segment_index": 0},
        {"text": "伪造ID结论", "quote": "真实片段", "segment_index": 0, "evidence_id": "fake-123"},
        {"text": "模型时间结论", "quote": "真实片段", "segment_index": 0, "start_ms": 999, "end_ms": 1999},
        {"text": "越范围结论", "quote": "某原文", "segment_index": 99},
        {"text": "不匹配结论", "quote": "不存在的原文", "segment_index": 0},
    ])
    job2_id = await organize_full(a2, v2, crafted)
    note2 = await fetch_note((await fetch_job(job2_id)).output_version)
    kcs2 = {c["text"]: c for c in note2.content["key_conclusions"]}
    valid_confirmed = kcs2["合法结论"]["status"] == "confirmed" and bool(kcs2["合法结论"]["evidence_ids"])
    fabricated_pending = all(
        kcs2[t]["status"] == "pending" and not kcs2[t]["evidence_ids"]
        for t in ("伪造ID结论", "模型时间结论", "越范围结论", "不匹配结论")
    )

    record(
        "P03-09",
        "证据由服务端解析 + 时间取自可信来源 + 非法引用降待确认不发布为已验证",
        time_from_seg and no_time_ok and valid_confirmed and fabricated_pending,
        dict(time_from_seg=time_from_seg, no_time_ok=no_time_ok,
             valid_status=kcs2["合法结论"]["status"], fabricated_pending=fabricated_pending),
    )


# ── P03-10 来源/状态独立于 Schema ───────────────────────────────────
async def p03_10(current, client):
    current["scope"] = None
    a1, _ = await create_lecture(SUBJECT_A, "p0310-a")
    _, v1 = await add_transcript(a1, [
        _seg(0, "教师明示的结论", start_ms=0, end_ms=1000),
        _seg(1, "【材料】材料推导结论", start_ms=2000, end_ms=3000),
        _seg(2, "【补充】模型补充结论", start_ms=4000, end_ms=5000),
        _seg(3, "【待确认】待确认结论", start_ms=6000, end_ms=7000),
    ])
    job_id = await organize_full(a1, v1, FakeOrganizeModel())
    note = await fetch_note((await fetch_job(job_id)).output_version)
    by_text = {c["text"]: c for c in note.content["key_conclusions"]}
    origins_ok = (
        by_text.get("教师明示的结论", {}).get("origin") == "teacher_explicit"
        and by_text.get("材料推导结论", {}).get("origin") == "material_derived"
        and by_text.get("模型补充结论", {}).get("origin") == "model_supplement"
        and by_text.get("待确认结论", {}).get("status") == "pending"
        and by_text.get("教师明示的结论", {}).get("status") == "confirmed"
    )

    # 无引用结论 → 不能混作已核对（降 pending）
    a2, _ = await create_lecture(SUBJECT_A, "p0310-b")
    _, v2 = await add_transcript(a2, [_seg(0, "真实片段", start_ms=0, end_ms=1000)])
    crafted = CraftedModel([
        {"text": "有引用结论", "quote": "真实片段", "segment_index": 0},
        {"text": "无引用结论", "quote": "无引用结论", "segment_index": None},
    ])
    job2_id = await organize_full(a2, v2, crafted)
    note2 = await fetch_note((await fetch_job(job2_id)).output_version)
    kcs2 = {c["text"]: c for c in note2.content["key_conclusions"]}
    no_ref_pending = kcs2["无引用结论"]["status"] == "pending" and not kcs2["无引用结论"]["evidence_ids"]
    has_ref_confirmed = kcs2["有引用结论"]["status"] == "confirmed" and bool(kcs2["有引用结论"]["evidence_ids"])

    record(
        "P03-10",
        "来源/确认状态保留且独立 + 无引用结论降待确认不混作已核对",
        origins_ok and no_ref_pending and has_ref_confirmed,
        dict(origins_ok=origins_ok, no_ref_pending=no_ref_pending, has_ref_confirmed=has_ref_confirmed),
    )


# ── P03-11 编辑/发布原子 + V1 晚完成不覆盖 V2 ─────────────────────────
async def p03_11(current, client):
    current["scope"] = None
    # (a) 版本链：V1 → N1 发布，V2 → N2 发布，N1 变 superseded 且证据保留
    a1, _ = await create_lecture(SUBJECT_A, "p0311-a")
    _, v1 = await add_transcript(a1, [_seg(0, "V1正文", start_ms=0, end_ms=1000)])
    job1 = await organize_full(a1, v1, FakeOrganizeModel())
    n1_id = (await fetch_job(job1)).output_version
    _, v2 = await add_transcript(a1, [_seg(0, "V2正文", start_ms=0, end_ms=1000)])
    job2 = await organize_full(a1, v2, FakeOrganizeModel())
    n2_id = (await fetch_job(job2)).output_version
    n1 = await fetch_note(n1_id)
    n2 = await fetch_note(n2_id)
    async with async_session_factory() as db:
        meeting = await db.get(Meeting, a1)
        n1_ev_count = (await db.execute(
            select(func.count(ArtifactEvidence.id)).where(ArtifactEvidence.artifact_id == n1_id)
        )).scalar_one()
    version_ok = (
        n1.status == "superseded" and n2.status == "published"
        and meeting.active_note_version_id == n2_id and n1_ev_count >= 1
    )
    # 新旧版本身份可分别读取
    current["scope"] = None
    current["subject_id"] = SUBJECT_A
    r_n1 = await client.get(f"/api/notes/{n1_id}")
    r_n2 = await client.get(f"/api/notes/{n2_id}")
    both_readable = r_n1.status_code == 200 and r_n2.status_code == 200 and r_n1.json()["status"] == "superseded"

    # (b) 编辑草稿：来源可追踪
    a2, _ = await create_lecture(SUBJECT_A, "p0311-b")
    _, v2a = await add_transcript(a2, [_seg(0, "草稿正文", start_ms=0, end_ms=1000)])
    fake_b = FakeOrganizeModel()
    fake_b.invalid_schema_chunks = {0}  # 造一个草稿
    job_b = await organize_full(a2, v2a, fake_b)
    n_b = (await fetch_job(job_b)).output_version
    r_edit = await client.patch(f"/api/notes/{n_b}", json={"title": "人工标题", "edit_source": {"kind": "user_edit"}})
    n_b_after = await fetch_note(n_b)
    edit_ok = r_edit.status_code == 200 and n_b_after.content["title"] == "人工标题" and n_b_after.edited_at is not None

    # (c) 发布切换提交前注入失败 → 回滚不留半发布结果（原子发布）
    a3, _ = await create_lecture(SUBJECT_A, "p0311-c")
    _, v3a = await add_transcript(a3, [_seg(0, "原子正文", start_ms=0, end_ms=1000)])
    job_c = await make_organize_job(a3, v3a)

    def _boom():
        raise RuntimeError("publish interrupted")

    org_module._publish_hook = _boom
    try:
        await run_organize_job(job_c, FakeOrganizeModel())
    finally:
        org_module._publish_hook = None
    async with async_session_factory() as db:
        jc = await db.get(ProcessingJob, job_c)
        m3 = await db.get(Meeting, a3)
        note_count_c = (await db.execute(
            select(func.count(NoteVersion.id)).where(NoteVersion.lecture_id == a3)
        )).scalar_one()
    # 会话回滚后不应留下半发布结果：任务终态失败、指针未切、笔记/索引意向未落库
    atomic_ok = jc.status == "failed" and m3.active_note_version_id is None and note_count_c == 0

    # (d) V1 任务先启动 → V2 笔记先发布 → V1 晚完成不覆盖 V2
    a4, _ = await create_lecture(SUBJECT_A, "p0311-d")
    _, v1d = await add_transcript(a4, [_seg(0, "V1D正文", start_ms=0, end_ms=1000)])
    job_v1 = await make_organize_job(a4, v1d)
    _, v2d = await add_transcript(a4, [_seg(0, "V2D正文", start_ms=0, end_ms=1000)])
    job_v2 = await organize_full(a4, v2d, FakeOrganizeModel())  # V2 先完成发布
    n2d = (await fetch_job(job_v2)).output_version
    await run_organize_job(job_v1, FakeOrganizeModel())  # V1 晚完成
    n1d = (await fetch_job(job_v1)).output_version
    async with async_session_factory() as db:
        m4 = await db.get(Meeting, a4)
    late_ok = (m4.active_note_version_id == n2d) and (await fetch_note(n1d)).status == "draft"

    record(
        "P03-11",
        "编辑留来源 + 发布原子 + 版本可追溯 + V1 晚完成不覆盖 V2",
        version_ok and both_readable and edit_ok and atomic_ok and late_ok,
        dict(version_ok=version_ok, both_readable=both_readable, edit_ok=edit_ok,
             atomic_ok=atomic_ok, late_ok=late_ok),
    )


# ── P03-12 分块失败只重试失败块 + 缺块不得完整发布 ─────────────────────
async def p03_12(current, client):
    current["scope"] = None
    a1, _ = await create_lecture(SUBJECT_A, "p0312-a")
    # 每块一个长片段（约 2000 字），4 个片段强制切出 4 个核心块
    segs = [_seg(i, f"块{i}内容" + "。" * 1990, start_ms=i * 5000, end_ms=(i + 1) * 5000 - 100) for i in range(4)]
    _, v1 = await add_transcript(a1, segs)

    fake = FakeOrganizeModel()
    fake.chunk_fail = {1: "retryable"}  # 中间块暂时失败
    job_id = await organize_full(a1, v1, fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    partial = (
        job.status == "failed" and job.error_code == "incomplete_coverage"
        and note.status == "draft" and note.content["coverage"]["complete"] is False
    )
    calls_after_partial = {c["chunk_index"] for c in fake.calls}
    # 成功块（0/2/3）各调用一次
    success_once = calls_after_partial == {0, 2, 3}

    # 缺块草稿实际尝试发布 → 拒绝正式发布，不切指针、不生成索引意向（P03-12 修正）
    current["subject_id"] = SUBJECT_A
    r_pub_partial = await client.post(f"/api/notes/{note.id}/publish")
    async with async_session_factory() as db:
        n_partial = await db.get(NoteVersion, note.id)
        m1_partial = await db.get(Meeting, a1)
        intent_partial = (await db.execute(
            select(func.count(IndexIntent.id)).where(IndexIntent.note_version_id == note.id)
        )).scalar_one()
    publish_rejected = (
        r_pub_partial.status_code == 409
        and n_partial.status == "draft"
        and m1_partial.active_note_version_id is None
        and intent_partial == 0
    )

    # 重试失败块：清除故障后覆盖满足 → 发布
    fake.chunk_fail = {}
    async with async_session_factory() as db:
        await organize_service.run_organize_job(db, job_id, model=fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    recovered = (
        job.status == "succeeded" and note.status == "published"
        and note.content["coverage"]["complete"] is True
    )
    calls_total = [c["chunk_index"] for c in fake.calls]
    reuse_ok = calls_total.count(0) == 1 and calls_total.count(2) == 1 and calls_total.count(3) == 1

    # 失败持续至上限 → 明确终态
    a2, _ = await create_lecture(SUBJECT_A, "p0312-b")
    _, v2 = await add_transcript(a2, segs)
    fake2 = FakeOrganizeModel()
    fake2.chunk_fail = {1: "retryable"}
    job2_id = await organize_full(a2, v2, fake2)
    job2 = await fetch_job(job2_id)
    async with async_session_factory() as db:
        manifest2 = (await db.execute(
            select(ChunkManifest).where(ChunkManifest.job_id == job2_id)
        )).scalars().first()
        failed_item = (await db.execute(
            select(ChunkItem).where(ChunkItem.manifest_id == manifest2.id, ChunkItem.chunk_index == 1)
        )).scalars().first()
        over_limit = job2.status == "failed" and failed_item.attempt == settings.ORGANIZE_MAX_ATTEMPTS

    record(
        "P03-12",
        "缺块不完整发布 + 缺块草稿拒绝正式发布 + 成功块复用 + 重试后覆盖满足才发布 + 超限明确终态",
        partial and publish_rejected and success_once and recovered and reuse_ok and over_limit,
        dict(partial=partial, publish_rejected=publish_rejected, success_once=success_once,
             recovered=recovered, reuse_ok=reuse_ok, over_limit=over_limit),
    )


# ── P03-13 恢复/重复执行/取消不重复产物或误发布 ────────────────────────
async def p03_13(current, client):
    current["scope"] = None
    a1, _ = await create_lecture(SUBJECT_A, "p0313-a")
    # 每块一个长片段（约 2000 字），3 个片段强制切出 3 个核心块
    segs = [_seg(i, f"恢复块{i}" + "。" * 1990, start_ms=i * 5000, end_ms=(i + 1) * 5000 - 100) for i in range(3)]
    _, v1 = await add_transcript(a1, segs)

    fake = FakeOrganizeModel()
    fake.crash_on_chunk = 1  # 首块成功后 worker 失联
    job_id = await make_organize_job(a1, v1)
    crashed = False
    try:
        await run_organize_job(job_id, fake)
    except WorkerCrash:
        crashed = True

    async with async_session_factory() as db:
        manifest = (await db.execute(
            select(ChunkManifest).where(ChunkManifest.job_id == job_id)
        )).scalars().first()
        item0 = (await db.execute(
            select(ChunkItem).where(ChunkItem.manifest_id == manifest.id, ChunkItem.chunk_index == 0)
        )).scalars().first()
        block0_persisted = item0.status == "succeeded" and item0.output is not None

    # 恢复：沿用已成功块，不重复调用
    fake.crash_on_chunk = None
    await run_organize_job(job_id, fake)
    job = await fetch_job(job_id)
    recovered_succeeded = job.status == "succeeded"
    calls = [c["chunk_index"] for c in fake.calls]
    reuse_ok = calls.count(0) == 1

    # 重复执行同一任务（幂等）→ 不重复产物
    async with async_session_factory() as db:
        await organize_service.run_organize_job(db, job_id, model=fake)
    async with async_session_factory() as db:
        note_count = (await db.execute(
            select(func.count(NoteVersion.id)).where(NoteVersion.lecture_id == a1)
        )).scalar_one()
    single_note = note_count == 1

    # 取消：运行中取消 → 迟到回执不切换正式笔记
    a2, _ = await create_lecture(SUBJECT_A, "p0313-b")
    _, v2 = await add_transcript(a2, [_seg(0, "取消正文", start_ms=0, end_ms=1000)])
    job2_id = await make_organize_job(a2, v2)
    async with async_session_factory() as db:
        j2 = await db.get(ProcessingJob, job2_id)
        j2.cancel_requested = True
        await db.commit()
    await run_organize_job(job2_id, FakeOrganizeModel())
    async with async_session_factory() as db:
        j2 = await db.get(ProcessingJob, job2_id)
        m2 = await db.get(Meeting, a2)
        cancelled = j2.status == "cancelled" and m2.active_note_version_id is None

    # 请求入口重试：同一整理业务请求两次 → 复用同一 job，不重复笔记/教师任务（P03-13 修正）
    a3, _ = await create_lecture(SUBJECT_A, "p0313-c")
    _, v3 = await add_transcript(a3, [
        _seg(0, "课堂正文", start_ms=0, end_ms=1000),
        _seg(1, "【作业】重试请求作业", start_ms=2000, end_ms=3000),
    ])
    current["subject_id"] = SUBJECT_A
    r1 = await client.post(f"/api/lectures/{a3}/organize", json={"transcript_version_id": str(v3)})
    r2 = await client.post(f"/api/lectures/{a3}/organize", json={"transcript_version_id": str(v3)})
    same_job = r1.status_code == 201 and r2.status_code == 201 and r1.json()["id"] == r2.json()["id"]
    await run_organize_job(uuid.UUID(r1.json()["id"]), FakeOrganizeModel())
    async with async_session_factory() as db:
        job_count = (await db.execute(
            select(func.count(ProcessingJob.id)).where(
                ProcessingJob.resource_id == a3, ProcessingJob.type == "organize"
            )
        )).scalar_one()
        notes3 = (await db.execute(
            select(NoteVersion).where(NoteVersion.lecture_id == a3)
        )).scalars().all()
    retry_idem = (
        same_job and job_count == 1 and len(notes3) == 1
        and len(notes3[0].content["tasks"]) == 1
    )

    record(
        "P03-13",
        "worker 失联恢复沿用已成功块 + 重复执行不重复产物 + 取消不误发布 + 请求入口重试复用",
        crashed and block0_persisted and recovered_succeeded and reuse_ok and single_note
        and cancelled and retry_idem,
        dict(crashed=crashed, block0_persisted=block0_persisted,
             recovered=recovered_succeeded, reuse_ok=reuse_ok, single_note=single_note,
             cancelled=cancelled, retry_idem=retry_idem, same_job=same_job),
    )


# ── P03-14 最小笔记阅读入口可回听正确来源 ─────────────────────────────
async def p03_14(current, client):
    current["scope"] = None
    a1, _ = await create_lecture(SUBJECT_A, "p0314-a")
    _, v1 = await add_transcript(a1, [
        _seg(0, "有时间事实", start_ms=3000, end_ms=5000),
        _seg(1, "缺时间事实", start_ms=None, end_ms=None),
    ])
    job_id = await organize_full(a1, v1, FakeOrganizeModel())
    note = await fetch_note((await fetch_job(job_id)).output_version)
    kcs = {c["text"]: c for c in note.content["key_conclusions"]}
    ev_time_id = kcs["有时间事实"]["evidence_ids"][0]
    ev_notime_id = kcs["缺时间事实"]["evidence_ids"][0]

    current["subject_id"] = SUBJECT_A
    r_time = await client.get(f"/api/evidence/{ev_time_id}")
    r_notime = await client.get(f"/api/evidence/{ev_notime_id}")
    time_ok = (
        r_time.status_code == 200 and r_time.json().get("source_type") == "transcript"
        and r_time.json().get("source_audio_asset_id") is not None
        and r_time.json().get("start_ms") == 3000 and r_time.json().get("end_ms") == 5000
    )
    notime_ok = (
        r_notime.status_code == 200 and r_notime.json().get("start_ms") is None
        and r_notime.json().get("end_ms") is None
        and r_notime.json().get("segment_start") == 1
        and r_notime.json().get("content") == ["缺时间事实"]
    )

    # 切换当前转写后，旧引用仍指旧版本且有历史提示
    _, v2 = await add_transcript(a1, [_seg(0, "新版本正文", start_ms=0, end_ms=1000)])
    r_old = await client.get(f"/api/evidence/{ev_time_id}")
    old_ok = (
        r_old.status_code == 200
        and r_old.json().get("source_version_id") == str(v1)
        and r_old.json().get("is_current") is False
    )

    # 越权访问：他主主体读取笔记 / 证据均 404（回听入口与引用定位同样受权限约束）
    current["scope"] = ScopeContext(subject_id=SUBJECT_B)
    r_b_note = await client.get(f"/api/notes/{note.id}")
    r_b_ev = await client.get(f"/api/evidence/{ev_time_id}")
    auth_ok = r_b_note.status_code == 404 and r_b_ev.status_code == 404
    current["scope"] = None

    # 音频回听端点：真实文件返回 200 audio/mpeg（回听不是仅靠时间字段替代）
    import tempfile
    audio_ok = False
    r_audio = None
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.write(b"ID3\x04\x00\x00\x00\x00\x00\x00fake-audio-bytes")
    tmp.close()
    try:
        async with async_session_factory() as db:
            meeting = await db.get(Meeting, a1)
            meeting.audio_url = tmp.name
            await db.commit()
        r_audio = await client.get(f"/api/meetings/{a1}/audio")
        audio_ok = (
            r_audio.status_code == 200
            and (r_audio.headers.get("content-type") or "").startswith("audio/mpeg")
        )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    # 前端最小页面证据：路由 + 页面 + 按证据固定资产回听逻辑存在。
    # 静态存在性只是烟测，实际解码/资产选择由 verify_phase3_note_browser.cjs 浏览器实测覆盖。
    fe_dir = ROOT.parent / "frontend" / "src"
    note_page = fe_dir / "features" / "notes" / "pages" / "note-detail-page.tsx"
    routes_file = fe_dir / "router" / "routes.tsx"
    page_src = note_page.read_text(encoding="utf-8") if note_page.exists() else ""
    routes_src = routes_file.read_text(encoding="utf-8") if routes_file.exists() else ""
    fe_ok = (
        note_page.exists()
        and "currentTime" in page_src and ".play()" in page_src
        and 'data-testid="note-audio"' in page_src
        and "notes/:noteId" in routes_src
        # 旧引用按证据固定资产回听，而非始终用课时当前音频
        and "source_audio_asset_id" in page_src and "/api/audio-assets/" in page_src
        # 源切换后等元数据就绪再定位播放
        and "onLoadedMetadata" in page_src
    )

    record(
        "P03-14",
        "有时间引用可回听正确来源 + 缺时间只定位原文 + 旧引用仍指旧版本 + 越权404 + 音频端点200 + 前端按固定资产回听逻辑存在",
        time_ok and notime_ok and old_ok and auth_ok and audio_ok and fe_ok,
        dict(time_ok=time_ok, notime_ok=notime_ok, old_ok=old_ok,
             auth_note=r_b_note.status_code, auth_ev=r_b_ev.status_code,
             audio_status=r_audio.status_code if r_audio is not None else None,
             audio_ctype=(r_audio.headers.get("content-type") or "") if r_audio is not None else "",
             fe_note_page=note_page.exists(),
             fe_routes=("notes/:noteId" in routes_src),
             fe_playback=("currentTime" in page_src and ".play()" in page_src),
             fe_fixed_asset=("source_audio_asset_id" in page_src and "/api/audio-assets/" in page_src),
             fe_metadata_wait=("onLoadedMetadata" in page_src),
             time_body=r_time.json() if r_time.status_code == 200 else None),
    )


# ── P03-15 发布后持久交接索引意向 ────────────────────────────────────
async def p03_15(current, client):
    current["scope"] = None
    a1, _ = await create_lecture(SUBJECT_A, "p0315-a")
    _, v1 = await add_transcript(a1, [_seg(0, "索引交接内容", start_ms=0, end_ms=1000)])
    job_id = await organize_full(a1, v1, FakeOrganizeModel())
    note = await fetch_note((await fetch_job(job_id)).output_version)
    async with async_session_factory() as db:
        intents = (await db.execute(
            select(IndexIntent).where(IndexIntent.note_version_id == note.id)
        )).scalars().all()
        intent_ok = len(intents) == 1 and intents[0].source_version_id == v1 and intents[0].status == "pending"

    # 重复发布请求 + 重复执行任务 → 不重复意向
    current["subject_id"] = SUBJECT_A
    await client.post(f"/api/notes/{note.id}/publish")
    async with async_session_factory() as db:
        await organize_service.run_organize_job(db, job_id, model=FakeOrganizeModel())
    async with async_session_factory() as db:
        intent_count = (await db.execute(
            select(func.count(IndexIntent.id)).where(IndexIntent.note_version_id == note.id)
        )).scalar_one()
    no_dup = intent_count == 1

    record(
        "P03-15",
        "发布后持久待索引意向 + 重复发布/执行不重复同一业务事件",
        intent_ok and no_dup,
        dict(intent_ok=intent_ok, intent_count=intent_count),
    )


# ── P03-16 新增结构与旧阶段数据兼容 ───────────────────────────────────
async def p03_16():
    # 路径 1：主库空库 → head
    conn = await asyncpg.connect(pg_dsn(MAIN_DB))
    try:
        main_head = await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()

    # 路径 2：阶段二收口 head 含资产/版本/证据样本 → 升级到 head，数据保留
    db2 = PREFIX + "p2data"
    DBS.append(db2)
    await create_db(db2)
    run_alembic("a2b3c4d5e6f7", asyncpg_dsn(db2))
    cid, mid, aid, vid, sid, eid = (uuid.uuid4() for _ in range(6))
    conn = await asyncpg.connect(pg_dsn(db2))
    try:
        await conn.execute(
            "INSERT INTO courses (id, owner_id, title, created_at, updated_at) VALUES ($1,$2,$3,now(),now())",
            cid, SUBJECT_A, "p2-course",
        )
        await conn.execute(
            "INSERT INTO meetings (id, course_id, title, status, legacy_meeting) "
            "VALUES ($1,$2,$3,'processed',false)",
            mid, cid, "p2-lecture",
        )
        await conn.execute(
            "INSERT INTO audio_assets (id, lecture_id, storage_key, version, is_demo, created_at) "
            "VALUES ($1,$2,$3,1,false,now())",
            aid, mid, "assets/old.wav",
        )
        await conn.execute(
            "INSERT INTO transcript_versions (id, audio_asset_id, lecture_id, version, provider, status, created_at) "
            "VALUES ($1,$2,$3,1,'real','active',now())",
            vid, aid, mid,
        )
        await conn.execute(
            "INSERT INTO transcript_segments (id, version_id, seq_index, speaker, content, start_ms, end_ms, created_at) "
            "VALUES ($1,$2,0,'a','旧片段',0,1000,now())",
            sid, vid,
        )
        await conn.execute(
            "INSERT INTO evidence_spans (id, transcript_version_id, segment_start, segment_end, is_valid, created_at) "
            "VALUES ($1,$2,0,0,true,now())",
            eid, vid,
        )
    finally:
        await conn.close()

    run_alembic("head", asyncpg_dsn(db2))
    conn = await asyncpg.connect(pg_dsn(db2))
    try:
        head = await conn.fetchval("SELECT version_num FROM alembic_version")
        kept = (
            await conn.fetchval("SELECT count(*) FROM courses WHERE id = $1", cid),
            await conn.fetchval("SELECT count(*) FROM meetings WHERE id = $1", mid),
            await conn.fetchval("SELECT count(*) FROM audio_assets WHERE id = $1", aid),
            await conn.fetchval("SELECT count(*) FROM transcript_versions WHERE id = $1", vid),
            await conn.fetchval("SELECT count(*) FROM transcript_segments WHERE id = $1", sid),
            await conn.fetchval("SELECT count(*) FROM evidence_spans WHERE id = $1", eid),
        )
        all_kept = all(k == 1 for k in kept)
        new_tables = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' "
            "AND table_name IN ('chunk_manifests','chunk_items','index_intents')"
        )
        note_col = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_name='note_versions' AND column_name='published_at'"
        )
    finally:
        await conn.close()

    record(
        "P03-16",
        "空库与阶段二 head 均升级到 phase3 head + 数据/ID/证据保留 + 新结构就位",
        main_head == "b3c4d5e6f7a8" and head == "b3c4d5e6f7a8"
        and all_kept and new_tables == 3 and note_col == 1,
        dict(main_head=main_head, head=head, kept=kept, new_tables=new_tables, note_col=note_col),
    )


if __name__ == "__main__":
    asyncio.run(main())
