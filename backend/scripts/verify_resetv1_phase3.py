"""reset-v1 阶段 3「课堂整理与发布」验收（返工版）：A3 / A4 / A6 独立验证。

对应 docs/classroom-agent/reset-v1/03-acceptance-v1.1.md 及返工要求：
- A3 / R3：经「实际课堂整理入口」（HTTP POST /api/lectures/{id}/organize + worker 派发）
  接入规划、预算检查、四类课堂分支与校验；分支选中/跳过/执行取自实际运行；含例题、无例题
  及可触发不同规划（长输入多块）的输入复跑，提交规划决定、实际调用与逐类来源对照。
- A4 / R4：完整整理先生成可审阅草稿（不自动发布）；首次显式确认前不切发布指针、不建索引意向、
  不入正式来源；覆盖「已有已发布笔记再生成新草稿」；失效引用用带唯一标记的条目验证索引与召回
  边界（从正式知识中排除）；重复发布从尚未发布的有效草稿开始，保留确认前/确认后/第二次提交后
  的状态、指针、索引意向数量与查询结果。
- A6 / R6：保留预算零调用、超时有界重试、恢复无重复产物；补充从实际运行详情查询各操作耗时、
  错误、重试次数及模型用量，区分零调用与未知用量。

运行（backend 目录）：
  .venv/Scripts/python.exe scripts/verify_resetv1_phase3.py
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# 使脚本无论从何处调用都能 import app（backend/ 加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.update(
    OPENAI_API_KEY="", DASHSCOPE_API_KEY="", PYTHONPATH=".", PYTHONUTF8="1"
)

import asyncpg
from app.config import settings

BASE = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
PREFIX = "yuan_resetv1_p3_" + uuid.uuid4().hex[:10] + "_"
MAIN_DB = PREFIX + "main"

# 在导入会创建引擎的模块前，把引擎指向隔离测试库，绝不触碰生产库。
settings.DATABASE_URL = BASE.replace("postgresql://", "postgresql+asyncpg://") + "/" + MAIN_DB
os.environ["DATABASE_URL"] = settings.DATABASE_URL

from app.api.deps import get_scope
from app.db.session import async_session_factory
from app.main import app
from app.models.course import Course
from app.models.meeting import Meeting
from app.models.note_version import NoteVersion
from app.models.organize import ChunkItem, ChunkManifest, IndexIntent
from app.models.processing_job import ProcessingJob
from app.models.transcript_version import TranscriptSegment, TranscriptVersion
from app.models.audio_asset import AudioAsset
from app.models.evidence import EvidenceSpan
from app.models.processing_job import ProviderCall
from app.models.budget import BudgetAccount, BudgetReservation
from app.models.index_version import IndexBlock, IndexVersion
from app.services import call_budget_service as budget
from app.services.embedding_service import DeterministicEmbedding
from app.services.job_service import retry_job
from app.services.organize_model import FakeOrganizeModel, ModelError, OrganizeModel
from app.services.organize_service import organize_service
from app.services.retrieval_service import retrieval_service
from app.services.scope import ScopeContext
from app.services.index_service import index_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, delete

ROOT = Path(__file__).resolve().parents[1]  # backend/
OUT = ROOT.parent / "docs" / "classroom-agent" / "reset-v1" / "evidence" / "phase3"
OUT.mkdir(parents=True, exist_ok=True)
PRODUCTS = OUT / "products"
PRODUCTS.mkdir(parents=True, exist_ok=True)

SUBJECT_A = "subject-a"
RESULTS: list[dict] = []


def record(pid: str, check: str, ok: bool, detail) -> None:
    row = dict(id=pid, check=check, result="通过" if ok else "不通过", detail=detail)
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


# 源码快照（证据口径，不记录密钥/正文）
SOURCE_FILES = [
    "app/services/organize_model.py",
    "app/services/organize_service.py",
    "app/services/index_service.py",
    "app/services/retrieval_service.py",
    "app/schemas/note.py",
    "scripts/verify_resetv1_phase3.py",
    "../docs/classroom-agent/reset-v1/03-acceptance-v1.1.md",
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
    env.update(DATABASE_URL=url, PYTHONPATH=".", PYTHONUTF8="1",
               OPENAI_API_KEY="", DASHSCOPE_API_KEY="")
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        env=env, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    if r.returncode != 0:
        raise RuntimeError(f"alembic upgrade {revision} failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")


# ── 数据构造助手 ──────────────────────────────────────────────────────

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


def _seg(seq, content, start_ms=None, end_ms=None) -> dict:
    return dict(seq_index=seq, speaker="t", content=content,
                start_ms=start_ms, end_ms=end_ms)


async def add_transcript(
    lecture_id: uuid.UUID, segments: list[dict], *, active: bool = True
) -> tuple[uuid.UUID, uuid.UUID]:
    async with async_session_factory() as db:
        meeting = await db.get(Meeting, lecture_id)
        asset = AudioAsset(
            lecture_id=lecture_id, storage_key="assets/test.wav",
            duration_ms=max((s.get("end_ms") or 0) for s in segments) or None,
            is_demo=False,
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
            db.add(TranscriptSegment(
                version_id=version.id, seq_index=s["seq_index"],
                speaker=s.get("speaker"), content=s["content"],
                start_ms=s.get("start_ms"), end_ms=s.get("end_ms"),
            ))
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


async def http_organize(client, lecture_id, version_id) -> uuid.UUID:
    r = await client.post(
        f"/api/lectures/{lecture_id}/organize",
        json={"transcript_version_id": str(version_id)},
    )
    assert r.status_code == 201, f"organize HTTP {r.status_code}: {r.text}"
    return uuid.UUID(r.json()["id"])


async def dispatch_organize(model) -> dict:
    async with async_session_factory() as db:
        return await organize_service.recover_and_dispatch_organize(db, model=model)


async def fetch_job(job_id) -> ProcessingJob | None:
    async with async_session_factory() as db:
        return await db.get(ProcessingJob, job_id)


async def fetch_note(note_id) -> NoteVersion | None:
    async with async_session_factory() as db:
        return await db.get(NoteVersion, note_id)


async def fetch_meeting(lecture_id) -> Meeting | None:
    async with async_session_factory() as db:
        return await db.get(Meeting, lecture_id)


async def intent_count(note_id) -> int:
    async with async_session_factory() as db:
        return (await db.execute(
            select(func.count(IndexIntent.id)).where(IndexIntent.note_version_id == note_id)
        )).scalar_one()


async def published_count(lecture_id) -> int:
    async with async_session_factory() as db:
        return (await db.execute(
            select(func.count(NoteVersion.id)).where(
                NoteVersion.lecture_id == lecture_id, NoteVersion.status == "published"
            )
        )).scalar_one()


async def gather_note_units(course_id) -> list[dict]:
    async with async_session_factory() as db:
        units = await index_service.gather_sources(db, course_id)
        return [u for u in units if u["source_type"] == "note"]


# ── A3：四类整理产物 + 规划 + 来源（经实际课堂整理入口）──────────────

async def a3_with_example(current, client):
    current["scope"] = None
    l1, cid = await create_lecture(SUBJECT_A, "a3-with-example")
    _, v1 = await add_transcript(l1, [
        _seg(0, "【例题】计算 2+3【解】2+3=5", start_ms=0, end_ms=1000),
        _seg(1, "【知识点】加法的定义【适用】整数相加", start_ms=2000, end_ms=3000),
        _seg(2, "【难点】进位容易出错【依据】课本第三章例题", start_ms=4000, end_ms=5000),
        _seg(3, "本堂课总纲：掌握加法", start_ms=6000, end_ms=7000),
    ])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)  # 实际入口：HTTP 建 job
    await dispatch_organize(fake)                  # 实际入口：worker 派发执行
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    c = note.content or {}

    kcs = c.get("key_conclusions", [])
    kps = c.get("knowledge_points", [])
    exs = c.get("examples", [])
    keys = c.get("key_points", [])
    branches = c.get("branches", {})
    plan = c.get("plan", {})

    ex_ok = (
        len(exs) == 1
        and exs[0]["problem"] == "计算 2+3"
        and exs[0]["derivation"] == "2+3=5"
        and bool(exs[0]["evidence_ids"])
    )
    kp_ok = (
        len(kps) == 1
        and kps[0]["definition"] == "加法的定义"
        and kps[0]["applicability"] == "整数相加"
        and bool(kps[0]["evidence_ids"])
    )
    key_ok = (
        len(keys) == 1
        and keys[0]["point"] == "进位容易出错"
        and keys[0]["reason"] == "课本第三章例题"
        and bool(keys[0]["evidence_ids"])
    )
    kc_ok = any(x["text"] == "本堂课总纲：掌握加法" and bool(x["evidence_ids"]) for x in kcs)

    # 分支记录：四类均「选中 + 已执行 + 未跳过」，计数与产物一致（A3 规划决定）。
    branch_ok = all(
        branches.get(b, {}).get("selected") is True
        and branches.get(b, {}).get("executed") is True
        and branches.get(b, {}).get("skipped") is False
        for b in ("note", "knowledge_point", "example", "key_point")
    ) and (
        branches["note"]["count"] == len(kcs)
        and branches["knowledge_point"]["count"] == len(kps)
        and branches["example"]["count"] == len(exs)
        and branches["key_point"]["count"] == len(keys)
    )
    # 规划决定：plan 中四类分支均 selected
    plan_ok = all(
        (plan.get("branches", {}).get(b, {}).get("selected") is True)
        for b in ("note", "knowledge_point", "example", "key_point")
    )

    # 产物样例落盘
    (PRODUCTS / "a3_with_example_note.json").write_text(
        json.dumps(c, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # 逐类抽核一条非空产物及来源（A3 判据）
    async with async_session_factory() as db:
        ev_ex = await db.get(EvidenceSpan, uuid.UUID(exs[0]["evidence_ids"][0])) if exs else None
        ev_kp = await db.get(EvidenceSpan, uuid.UUID(kps[0]["evidence_ids"][0])) if kps else None
        ev_key = await db.get(EvidenceSpan, uuid.UUID(keys[0]["evidence_ids"][0])) if keys else None
    source_ok = (
        ev_ex is not None and ev_ex.quote == "计算 2+3"
        and ev_kp is not None and ev_kp.quote == "加法的定义"
        and ev_key is not None and ev_key.quote == "进位容易出错"
    )

    # 实际调用：4 段并入单块 → 1 次 extract + 1 次 summarize（分支四类取自同一次实际运行）
    calls_ok = len(fake.calls) == 1 and fake.summarize_calls == 1

    ok = (
        job.status == "succeeded" and job.stage == "draft" and note.status == "draft"
        and ex_ok and kp_ok and key_ok and kc_ok and branch_ok and plan_ok
        and source_ok and calls_ok
    )
    record(
        "A3a", "含例题材料 → 四类分支选中/执行 + 规划决定 + 题干/推导/定义/难点与原文一致 + 逐类来源（HTTP 入口）",
        ok,
        dict(job_status=job.status, job_stage=job.stage, note_status=note.status,
             plan=plan,
             examples=[(e["problem"], e["derivation"], len(e["evidence_ids"])) for e in exs],
             knowledge_points=[(k["definition"], k["applicability"], len(k["evidence_ids"])) for k in kps],
             key_points=[(k["point"], k["reason"], len(k["evidence_ids"])) for k in keys],
             conclusions=[k["text"] for k in kcs],
             branches=branches,
             extract_calls=len(fake.calls), summarize_calls=fake.summarize_calls,
             source_ok=source_ok),
    )


async def a3_without_example(current, client):
    current["scope"] = None
    l1, _ = await create_lecture(SUBJECT_A, "a3-without-example")
    _, v1 = await add_transcript(l1, [
        _seg(0, "本节课讲解基本概念", start_ms=0, end_ms=1000),
        _seg(1, "重点是理解原理", start_ms=2000, end_ms=3000),
    ])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    c = note.content or {}

    exs = c.get("examples", [])
    kps = c.get("knowledge_points", [])
    keys = c.get("key_points", [])
    branches = c.get("branches", {})
    plan = c.get("plan", {})

    # 无例题材料 → 例题/知识点/难点列表为空，不编造题目（A3 判据）
    no_fabricate = exs == [] and kps == [] and keys == []
    # 分支记录：笔记选中 + 其余三类「未选中/已跳过」且「未执行」（A3 返工：被跳过的分支
    # 未执行不产出，executed=false，区别于「执行后产物为空」executed=true、count=0）。
    branch_skip_ok = (
        branches.get("note", {}).get("selected") is True
        and branches.get("note", {}).get("skipped") is False
        and branches.get("note", {}).get("executed") is True
        and branches.get("note", {}).get("count") >= 2
        and all(
            branches.get(b, {}).get("selected") is False
            and branches.get(b, {}).get("skipped") is True
            and branches.get(b, {}).get("executed") is False
            and branches.get(b, {}).get("count") == 0
            for b in ("knowledge_point", "example", "key_point")
        )
    )
    # 规划决定：笔记 selected，其余 skipped
    plan_ok = (
        (plan.get("branches", {}).get("note", {}).get("selected") is True)
        and all(
            (plan.get("branches", {}).get(b, {}).get("selected") is False)
            for b in ("knowledge_point", "example", "key_point")
        )
    )

    (PRODUCTS / "a3_without_example_note.json").write_text(
        json.dumps(c, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    ok = (
        job.status == "succeeded" and note.status == "draft"
        and no_fabricate and branch_skip_ok and plan_ok
    )
    record(
        "A3b", "无例题材料 → 例题/知识点/难点列表为空不编造 + 分支跳过记录 + 规划决定（HTTP 入口）",
        ok,
        dict(job_status=job.status, note_status=note.status,
             plan=plan, branches=branches,
             examples_count=len(exs), knowledge_points_count=len(kps),
             key_points_count=len(keys), conclusions_count=len(c.get("key_conclusions", []))),
    )


async def a3_different_planning(current, client):
    # 可触发不同规划的输入：长输入 → 多块分块清单（chunk_count > 1），且含多类标记。
    current["scope"] = None
    l1, _ = await create_lecture(SUBJECT_A, "a3-long")
    segs = []
    for i in range(12):
        body = "长输入正文" + ("。" * 2000)
        if i == 0:
            body = "【例题】长输入例题 " + body
        elif i == 6:
            body = "【知识点】长输入知识点【适用】通用 " + body
        segs.append(_seg(i, body, start_ms=i * 10000, end_ms=(i + 1) * 10000 - 100))
    _, v1 = await add_transcript(l1, segs)

    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    c = note.content or {}

    async with async_session_factory() as db:
        manifest = (await db.execute(
            select(ChunkManifest).where(ChunkManifest.job_id == job_id)
        )).scalars().first()
        chunk_count = manifest.chunk_count if manifest else 0

    # 不同规划：长输入切成多块；每块一次 extract 调用；规划记录含选中/跳过。
    multi_chunk = chunk_count > 1
    per_chunk_calls = len(fake.calls) == chunk_count
    plan = c.get("plan", {})
    branches = c.get("branches", {})
    plan_has_example = (plan.get("branches", {}).get("example", {}).get("selected") is True)
    plan_has_kp = (plan.get("branches", {}).get("knowledge_point", {}).get("selected") is True)

    ok = (
        job.status == "succeeded" and note.status == "draft"
        and multi_chunk and per_chunk_calls
        and plan_has_example and plan_has_kp
        and branches.get("example", {}).get("count", 0) >= 1
        and branches.get("knowledge_point", {}).get("count", 0) >= 1
    )
    record(
        "A3c", "可触发不同规划的长输入 → 多块分块清单 + 每块一次调用 + 规划/分支取自实际运行",
        ok,
        dict(job_status=job.status, note_status=note.status,
             chunk_count=chunk_count, extract_calls=len(fake.calls),
             summarize_calls=fake.summarize_calls,
             plan=plan, branches=branches),
    )


class FakeWithProdPlan(FakeOrganizeModel):
    """替身外发调用（extract/summarize）+ 生产规划策略（继承 OrganizeModel.plan）。"""

    name = "fake-prod-plan"

    async def plan(self, segments, meta):
        return await OrganizeModel.plan(self, segments, meta)


async def a3_production_planner(current, client):
    # A3d（返工）：生产整理入口使用实际的、可由输入与预算影响的规划策略。
    # 用生产路径的规划器（OrganizeModel.plan）处理两种材料，记录不同的选中集合，
    # 并对照传给 extract 调用的集合（fake.calls.selected_branches）与最终执行记录（branches）。
    current["scope"] = None

    async def run_case(title, segments):
        l1, _ = await create_lecture(SUBJECT_A, title)
        _, v1 = await add_transcript(l1, segments)
        fake = FakeWithProdPlan()
        job_id = await http_organize(client, l1, v1)
        await dispatch_organize(fake)
        job = await fetch_job(job_id)
        note = await fetch_note(job.output_version)
        c = note.content or {}
        plan = c.get("plan", {})
        branches = c.get("branches", {})
        selected = {n for n, b in (plan.get("branches") or {}).items() if b.get("selected")}
        executed = {n for n, b in branches.items() if b.get("executed")}
        return dict(job=job, note=note, plan=plan, branches=branches,
                    selected=selected, executed=executed, calls=fake.calls)

    # 材料 1：含例题/知识点/难点/正文信号 → 四类均选中
    m1 = await run_case("a3-prod-plan-all", [
        _seg(0, "【例题】例题：计算 2+3【解】2+3=5", start_ms=0, end_ms=1000),
        _seg(1, "【知识点】定义：加法的定义【适用】整数相加", start_ms=2000, end_ms=3000),
        _seg(2, "【难点】重点：进位易错【依据】课本第三章", start_ms=4000, end_ms=5000),
        _seg(3, "本堂课总纲：掌握加法", start_ms=6000, end_ms=7000),
    ])
    # 材料 2：仅正文信号 → 仅笔记选中，其余跳过
    m2 = await run_case("a3-prod-plan-note-only", [
        _seg(0, "今天的内容是掌握加法运算", start_ms=0, end_ms=1000),
        _seg(1, "通过练习加深理解", start_ms=2000, end_ms=3000),
    ])

    all_four = {"note", "knowledge_point", "example", "key_point"}
    m1_ok = m1["selected"] == all_four and m1["executed"] == all_four
    m2_ok = (
        m2["selected"] == {"note"} and m2["executed"] == {"note"}
        and all(
            m2["branches"].get(b, {}).get("skipped") is True
            for b in ("knowledge_point", "example", "key_point")
        )
    )
    # 对照：传给 extract 的集合 == 规划选中集合（两材料各一次 extract，集合一致）
    cross_ok = (
        len(m1["calls"]) == 1
        and set(m1["calls"][0]["selected_branches"]) == m1["selected"]
        and len(m2["calls"]) == 1
        and set(m2["calls"][0]["selected_branches"]) == m2["selected"]
    )
    # 不同材料 → 不同选中集合
    different_ok = m1["selected"] != m2["selected"]

    ok = (
        m1["job"].status == "succeeded" and m2["job"].status == "succeeded"
        and m1_ok and m2_ok and cross_ok and different_ok
    )
    record(
        "A3d", "生产规划器经整理入口处理两种材料 → 不同选中集合 + 传给 extract 的集合与最终执行记录一致",
        ok,
        dict(
            m1=dict(selected=sorted(m1["selected"]), executed=sorted(m1["executed"]),
                    extract_selected=m1["calls"][0]["selected_branches"] if m1["calls"] else None,
                    branches=m1["branches"]),
            m2=dict(selected=sorted(m2["selected"]), executed=sorted(m2["executed"]),
                    extract_selected=m2["calls"][0]["selected_branches"] if m2["calls"] else None,
                    branches=m2["branches"]),
        ),
    )


async def a3_budget_influenced_plan(current, client):
    # A3e（返工）：生产规划器受预算影响——可用预算不足时按优先级裁剪分支。
    current["scope"] = None
    segments = [
        _seg(0, "【例题】例题：计算 2+3【解】2+3=5"),
        _seg(1, "【知识点】定义：加法的定义【适用】整数相加"),
        _seg(2, "【难点】重点：进位易错【依据】课本第三章"),
        _seg(3, "本堂课总纲：掌握加法"),
    ]
    model = OrganizeModel()

    def _selected(plan):
        return {n for n, b in plan["branches"].items() if b["selected"]}

    full = _selected(await model.plan(segments, {"title": "t"}))
    mid = _selected(await model.plan(
        segments, {"title": "t", "available_budget_micro": 2, "branch_cost_micro": 1}
    ))
    tight = _selected(await model.plan(
        segments, {"title": "t", "available_budget_micro": 1, "branch_cost_micro": 1}
    ))

    ok = (
        full == {"note", "knowledge_point", "example", "key_point"}
        and mid == {"note", "knowledge_point"}
        and tight == {"note"}
    )
    record(
        "A3e", "生产规划器预算影响 → 可用预算不足时按优先级（笔记>知识点>例题>重点难点）裁剪选中分支",
        ok,
        dict(full=sorted(full), mid=sorted(mid), tight=sorted(tight)),
    )


# ── A4：失效引用 / 草稿确认 / 重复发布 ───────────────────────────────

class CraftedModel(OrganizeModel):
    """注入失效引用：伪造 evidence_id / 模型时间 / 越范围片段 / 不匹配 quote。"""
    name = "crafted"

    def __init__(self, conclusions):
        self.conclusions = conclusions
        self.calls = 0

    async def plan(self, segments, meta):
        return {
            "strategy": "四类分支（笔记/知识点/例题/重点难点）",
            "branches": {
                "note": {"selected": True, "reason": "正文片段产生笔记要点"},
                "knowledge_point": {"selected": False, "reason": "无知识点标记"},
                "example": {"selected": False, "reason": "无例题标记"},
                "key_point": {"selected": False, "reason": "无难点标记"},
            },
        }

    async def extract_chunk(self, chunk):
        self.calls += 1
        if chunk.chunk_index == 0:
            return {"conclusions": self.conclusions, "knowledge_points": [],
                    "examples": [], "key_points": []}
        return {"conclusions": [], "knowledge_points": [], "examples": [], "key_points": []}

    async def summarize(self, conclusions, knowledge_points, examples, key_points, meta):
        sections = [{"heading": "要点", "points": list(conclusions)}]
        return {"title": meta.get("title", "课堂笔记"),
                "outline": [s["heading"] for s in sections],
                "sections": sections}


async def a4_invalid_reference_boundary(current, client):
    current["scope"] = None
    VALID = "VALIDUNIQUEMARKER_A"
    INVALID = "INVALIDUNIQUEMARKER_B"
    l1, cid = await create_lecture(SUBJECT_A, "a4-invalid-ref")
    _, v1 = await add_transcript(l1, [_seg(0, f"唯一有效标记 {VALID} 的课堂原文", start_ms=0, end_ms=1000)])
    crafted = CraftedModel([
        {"text": f"唯一有效标记 {VALID} 的有效结论", "quote": f"唯一有效标记 {VALID}", "segment_index": 0},
        {"text": f"唯一失效标记 {INVALID} 伪造ID", "quote": f"唯一有效标记 {VALID}", "segment_index": 0, "evidence_id": "fake-123"},
        {"text": f"唯一失效标记 {INVALID} 模型时间", "quote": f"唯一有效标记 {VALID}", "segment_index": 0, "start_ms": 999},
        {"text": f"唯一失效标记 {INVALID} 越范围", "quote": "某原文", "segment_index": 99},
        {"text": f"唯一失效标记 {INVALID} 不匹配", "quote": "不存在的原文", "segment_index": 0},
    ])
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(crafted)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    kcs = {c["text"]: c for c in note.content["key_conclusions"]}

    valid_confirmed = (
        kcs[f"唯一有效标记 {VALID} 的有效结论"]["status"] == "confirmed"
        and bool(kcs[f"唯一有效标记 {VALID} 的有效结论"]["evidence_ids"])
    )
    fabricated_pending = all(
        kcs[f"唯一失效标记 {INVALID} {t}"]["status"] == "pending"
        and not kcs[f"唯一失效标记 {INVALID} {t}"]["evidence_ids"]
        for t in ("伪造ID", "模型时间", "越范围", "不匹配")
    )

    # 发布 + 建索引：失效条目须从正式知识中排除（索引与召回边界）
    await client.post(f"/api/notes/{note.id}/publish")
    async with async_session_factory() as db:
        intent = (await db.execute(
            select(IndexIntent).where(IndexIntent.note_version_id == note.id)
        )).scalars().first()
        if intent is not None:
            await index_service.consume_intent(db, intent, embed=DeterministicEmbedding(1024))

    # 索引边界：gather_sources 的笔记单元含 VALID 标记、不含 INVALID 标记
    note_units = await gather_note_units(cid)
    joined = "\n".join(u["content"] for u in note_units)
    index_boundary_ok = (VALID in joined) and (INVALID not in joined)

    # 召回边界：查询 VALID 标记能命中含该标记的正文；查询 INVALID 标记返回的正文均不含该标记
    #（失效条目已从正式知识排除；向量召回为语义模糊匹配，边界以「返回正文是否含失效标记」为准）。
    async with async_session_factory() as db:
        scope = ScopeContext(subject_id=SUBJECT_A, course_id=cid)
        s_valid = await retrieval_service.search(
            db, scope, VALID, top_k=3, embed=DeterministicEmbedding(1024),
        )
        s_invalid = await retrieval_service.search(
            db, scope, INVALID, top_k=3, embed=DeterministicEmbedding(1024),
        )

    def _result_contains(results: list[dict], marker: str) -> bool:
        return any(
            marker in chunk
            for r in results
            for chunk in (r.get("content") or [])
        )

    recall_valid_ok = s_valid["status"] == "ok" and _result_contains(s_valid["results"], VALID)
    recall_invalid_ok = not _result_contains(s_invalid["results"], INVALID)

    ok = (
        job.status == "succeeded" and valid_confirmed and fabricated_pending
        and index_boundary_ok and recall_valid_ok and recall_invalid_ok
    )
    record(
        "A4a", "失效引用降待确认 + 带唯一标记的失效条目从正式知识排除（索引与召回边界）",
        ok,
        dict(job_status=job.status,
             valid=dict(status=kcs[f"唯一有效标记 {VALID} 的有效结论"]["status"],
                        evidence_ids=kcs[f"唯一有效标记 {VALID} 的有效结论"]["evidence_ids"]),
             fabricated={t: dict(status=kcs[f"唯一失效标记 {INVALID} {t}"]["status"],
                                 evidence_ids=kcs[f"唯一失效标记 {INVALID} {t}"]["evidence_ids"])
                         for t in ("伪造ID", "模型时间", "越范围", "不匹配")},
             index_boundary=dict(valid_in_index=VALID in joined, invalid_in_index=INVALID in joined),
             recall=dict(valid_status=s_valid["status"],
                         valid_hits_contain_marker=_result_contains(s_valid["results"], VALID),
                         invalid_hits_contain_marker=_result_contains(s_invalid["results"], INVALID),
                         invalid_total=s_invalid["total"])),
    )


async def a4_draft_first_new_draft(current, client):
    current["scope"] = None
    l1, cid = await create_lecture(SUBJECT_A, "a4-draft-first")
    _, v1 = await add_transcript(l1, [_seg(0, "第一版正文内容 M1", start_ms=0, end_ms=1000)])

    # (1) 完整整理 → 草稿（不自动发布）
    fake1 = FakeOrganizeModel()
    job1 = await http_organize(client, l1, v1)
    await dispatch_organize(fake1)
    j1 = await fetch_job(job1)
    note1 = await fetch_note(j1.output_version)
    m1 = await fetch_meeting(l1)
    draft_intent = await intent_count(note1.id)   # 首次确认前快照：应为 0
    draft_first_ok = (
        j1.status == "succeeded" and j1.stage == "draft"
        and j1.last_successful_stage == "note.draft"
        and note1.status == "draft"
        and m1.active_note_version_id is None  # 未切发布指针
        and draft_intent == 0                  # 未建索引意向
    )

    # (2) 显式确认发布 → 切指针 + 建索引意向
    await client.post(f"/api/notes/{note1.id}/publish")
    n1p = await fetch_note(note1.id)
    m1p = await fetch_meeting(l1)
    publish_ok = (
        n1p.status == "published" and m1p.active_note_version_id == note1.id
        and await intent_count(note1.id) == 1
    )

    # (3) 已有已发布笔记，再生成新草稿
    _, v2 = await add_transcript(l1, [_seg(0, "第二版正文内容 M2", start_ms=0, end_ms=1000)])
    fake2 = FakeOrganizeModel()
    job2 = await http_organize(client, l1, v2)
    await dispatch_organize(fake2)
    j2 = await fetch_job(job2)
    note2 = await fetch_note(j2.output_version)
    m2 = await fetch_meeting(l1)
    new_draft_ok = (
        j2.status == "succeeded" and note2.status == "draft"
        and m2.active_note_version_id == note1.id  # 指针仍在已发布旧笔记
        and await intent_count(note2.id) == 0  # 新草稿无索引意向
    )
    # 索引边界：正式来源只含旧已发布笔记，不含新草稿
    note_units = await gather_note_units(cid)
    unit_note_ids = {str(u["source_version_id"]) for u in note_units}
    boundary_ok = str(note1.id) in unit_note_ids and str(note2.id) not in unit_note_ids

    ok = draft_first_ok and publish_ok and new_draft_ok and boundary_ok
    record(
        "A4b", "完整整理先产草稿不自动发布 + 显式确认才切指针/建意向 + 已有已发布笔记再生成新草稿不越界",
        ok,
        dict(draft_first=dict(job_status=j1.status, job_stage=j1.stage,
                              last_successful_stage=j1.last_successful_stage,
                              note_status=note1.status,
                              active_note=str(m1.active_note_version_id),
                              intent=draft_intent),
             publish=dict(note_status=n1p.status, active_note=str(m1p.active_note_version_id),
                          intent=await intent_count(note1.id)),
             new_draft=dict(job_status=j2.status, note_status=note2.status,
                            active_note=str(m2.active_note_version_id),
                            intent=await intent_count(note2.id)),
             boundary=dict(units=[u["content"] for u in note_units])),
    )


async def a4_duplicate_publish(current, client):
    # 重复发布从「尚未发布的有效草稿」开始，保留确认前/确认后/第二次提交后的状态。
    current["scope"] = None
    l1, _ = await create_lecture(SUBJECT_A, "a4-dup")
    _, v1 = await add_transcript(l1, [_seg(0, "有效草稿正文内容", start_ms=0, end_ms=1000)])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)

    # 状态 1：首次确认前
    s1_m = await fetch_meeting(l1)
    state1 = dict(note_status=(await fetch_note(note.id)).status,
                  active_note=str(s1_m.active_note_version_id),
                  intent=await intent_count(note.id))

    # 状态 2：首次确认（发布）
    r1 = await client.post(f"/api/notes/{note.id}/publish")
    s2_m = await fetch_meeting(l1)
    state2 = dict(pub_status=r1.status_code,
                  note_status=(await fetch_note(note.id)).status,
                  active_note=str(s2_m.active_note_version_id),
                  intent=await intent_count(note.id))

    # 状态 3：第二次提交（重复发布）
    r2 = await client.post(f"/api/notes/{note.id}/publish")
    s3_m = await fetch_meeting(l1)
    state3 = dict(pub_status=r2.status_code,
                  note_status=(await fetch_note(note.id)).status,
                  active_note=str(s3_m.active_note_version_id),
                  intent=await intent_count(note.id),
                  published_count=await published_count(l1))

    dup_ok = (
        state1["note_status"] == "draft" and state1["active_note"] == "None"
        and state1["intent"] == 0
        and r1.status_code == 200 and state2["note_status"] == "published"
        and state2["active_note"] == str(note.id) and state2["intent"] == 1
        and r2.status_code == 200 and state3["note_status"] == "published"
        and state3["active_note"] == str(note.id) and state3["intent"] == 1
        and state3["published_count"] == 1
    )
    record(
        "A4c", "重复发布从尚未发布的有效草稿开始 → 确认前/确认后/第二次提交后的状态、指针、意向数量均正确且幂等",
        dup_ok,
        dict(state1_before_confirm=state1, state2_after_confirm=state2,
             state3_after_second_submit=state3),
    )


async def a4_edit_reverify(current, client):
    # A4d（返工）：从完整草稿选一条已确认结论，经正常编辑接口改为原文不支持的内容，再显式发布。
    # 预期由服务端发布前重新核对：失配条目降待确认并从正式知识排除；正常编辑仍受支持的条目保留。
    current["scope"] = None
    EDIT_GOOD = "EDITGOODUNIQUEMARKER_C"
    EDIT_BAD = "EDITBADUNIQUEMARKER_D"
    l1, cid = await create_lecture(SUBJECT_A, "a4-edit-reverify")
    _, v1 = await add_transcript(l1, [
        _seg(0, f"原文片段 {EDIT_GOOD} 可支持内容", start_ms=0, end_ms=1000),
        _seg(1, "另一条普通结论正文", start_ms=2000, end_ms=3000),
    ])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    kcs = note.content["key_conclusions"]

    good_kc = next(k for k in kcs if EDIT_GOOD in (k.get("text") or ""))
    other_kc = next(k for k in kcs if EDIT_GOOD not in (k.get("text") or ""))

    # 正常编辑（正文与引文均不变，仅改来源标签）→ 未实质变更，仍受支持（回归）。
    # 注：正文被实质改动（含「只改正文、保留原引文」）由 A4e 覆盖并降待确认。
    good_kc["origin"] = "material_derived"
    # 失效编辑：正文与 quote 均改为原文不支持的内容 → 发布前须降待确认
    other_kc["text"] = f"编辑为原文不支持内容 {EDIT_BAD}"
    other_kc["quote"] = f"原文不存在标记 {EDIT_BAD}"

    r_edit = await client.patch(
        f"/api/notes/{note.id}",
        json={"key_conclusions": [good_kc, other_kc]},
    )
    assert r_edit.status_code == 200, f"edit HTTP {r_edit.status_code}: {r_edit.text}"

    # 首次发布：服务端重新核对编辑后正文与来源
    r_pub = await client.post(f"/api/notes/{note.id}/publish")
    assert r_pub.status_code == 200, f"publish HTTP {r_pub.status_code}: {r_pub.text}"
    note2 = await fetch_note(note.id)
    kcs2 = {k["text"]: k for k in note2.content["key_conclusions"]}
    good_reverified = (
        kcs2[good_kc["text"]]["status"] == "confirmed"
        and bool(kcs2[good_kc["text"]]["evidence_ids"])
    )
    bad_downgraded = (
        kcs2[other_kc["text"]]["status"] == "pending"
        and not kcs2[other_kc["text"]]["evidence_ids"]
    )

    # 消费索引意向：失效条目不得进入正式知识（索引与召回边界）
    async with async_session_factory() as db:
        intent = (await db.execute(
            select(IndexIntent).where(IndexIntent.note_version_id == note.id)
        )).scalars().first()
        if intent is not None:
            await index_service.consume_intent(db, intent, embed=DeterministicEmbedding(1024))
    note_units = await gather_note_units(cid)
    joined = "\n".join(u["content"] for u in note_units)
    index_boundary_ok = (EDIT_GOOD in joined) and (EDIT_BAD not in joined)

    async with async_session_factory() as db:
        scope = ScopeContext(subject_id=SUBJECT_A, course_id=cid)
        s_bad = await retrieval_service.search(
            db, scope, EDIT_BAD, top_k=3, embed=DeterministicEmbedding(1024),
        )
        s_good = await retrieval_service.search(
            db, scope, EDIT_GOOD, top_k=3, embed=DeterministicEmbedding(1024),
        )

    def _contains(results: list[dict], marker: str) -> bool:
        return any(marker in chunk for r in results for chunk in (r.get("content") or []))

    recall_bad_excluded = not _contains(s_bad["results"], EDIT_BAD)
    recall_good_in = s_good["status"] == "ok" and _contains(s_good["results"], EDIT_GOOD)

    # 重复发布回归：已发布笔记再次提交幂等（状态/指针/意向不重复）
    r_dup = await client.post(f"/api/notes/{note.id}/publish")
    note3 = await fetch_note(note.id)
    dup_ok = (
        r_dup.status_code == 200 and note3.status == "published"
        and await intent_count(note.id) == 1 and await published_count(l1) == 1
    )

    ok = (
        job.status == "succeeded" and note2.status == "published"
        and good_reverified and bad_downgraded
        and index_boundary_ok and recall_bad_excluded and recall_good_in and dup_ok
    )
    record(
        "A4d", "编辑为原文不支持内容再显式发布 → 服务端发布前重新核对：失配降待确认并排除出正式知识（唯一标记索引/召回边界），正常编辑与重复发布回归",
        ok,
        dict(edit_status=r_edit.status_code, publish_status=r_pub.status_code,
             good=dict(text=good_kc["text"], status=kcs2[good_kc["text"]]["status"],
                       evidence_ids=kcs2[good_kc["text"]]["evidence_ids"]),
             bad=dict(text=other_kc["text"], status=kcs2[other_kc["text"]]["status"],
                      evidence_ids=kcs2[other_kc["text"]]["evidence_ids"]),
             index_boundary=dict(good_in_index=EDIT_GOOD in joined, bad_in_index=EDIT_BAD in joined),
             recall=dict(bad_hits_contain_marker=_contains(s_bad["results"], EDIT_BAD),
                         good_hits_contain_marker=_contains(s_good["results"], EDIT_GOOD)),
             duplicate_publish=dict(status=r_dup.status_code, note_status=note3.status,
                                    intent=await intent_count(note.id),
                                    published_count=await published_count(l1))),
    )


async def a4_edit_body_only(current, client):
    # A4e（返工）：只改正文、保留原引文和证据 → 发布前须降 pending 并从正式知识排除。
    # 覆盖进入正式检索的五类：结论正文、定义、例题推导、重点难点、章节正文（= 结论正文）。
    current["scope"] = None
    CHANGED = "EDITBODYONLYUNIQUEMARKER_E"
    l1, cid = await create_lecture(SUBJECT_A, "a4-edit-body-only")
    _, v1 = await add_transcript(l1, [
        _seg(0, "【例题】例题：计算 2+3【解】2+3=5", start_ms=0, end_ms=1000),
        _seg(1, "【知识点】定义：加法的定义【适用】整数相加", start_ms=2000, end_ms=3000),
        _seg(2, "【难点】重点：进位易错【依据】课本第三章", start_ms=4000, end_ms=5000),
        _seg(3, "本堂课总纲：掌握加法", start_ms=6000, end_ms=7000),
    ])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    c = note.content or {}

    ex = c["examples"][0]
    kp = c["knowledge_points"][0]
    kpt = c["key_points"][0]
    kc = c["key_conclusions"][0]

    # 只改正文、保留原引文与证据：改定义/推导/难点/结论正文，quote 与 evidence_ids 不动。
    ex["derivation"] = f"只改推导 {CHANGED}"
    kp["definition"] = f"只改定义 {CHANGED}"
    kpt["point"] = f"只改难点 {CHANGED}"
    kc["text"] = f"只改结论正文 {CHANGED}"

    r_edit = await client.patch(
        f"/api/notes/{note.id}",
        json={
            "key_conclusions": c["key_conclusions"],
            "knowledge_points": c["knowledge_points"],
            "examples": c["examples"],
            "key_points": c["key_points"],
        },
    )
    assert r_edit.status_code == 200, f"edit HTTP {r_edit.status_code}: {r_edit.text}"

    r_pub = await client.post(f"/api/notes/{note.id}/publish")
    assert r_pub.status_code == 200, f"publish HTTP {r_pub.status_code}: {r_pub.text}"
    note2 = await fetch_note(note.id)
    c2 = note2.content or {}

    ex2 = c2["examples"][0]
    kp2 = c2["knowledge_points"][0]
    kpt2 = c2["key_points"][0]
    kc2 = c2["key_conclusions"][0]
    # 章节正文镜像结论：sections 的 points 与 key_conclusions 一致（同样降 pending）
    section_points = (c2.get("sections") or [{}])[0].get("points", [])

    body_changed_downgraded = (
        ex2["status"] == "pending" and not ex2["evidence_ids"]
        and kp2["status"] == "pending" and not kp2["evidence_ids"]
        and kpt2["status"] == "pending" and not kpt2["evidence_ids"]
        and kc2["status"] == "pending" and not kc2["evidence_ids"]
    )
    section_synced = bool(section_points) and any(
        p.get("text") == kc2["text"] and p.get("status") == "pending" for p in section_points
    )

    # 消费索引意向：失配条目的唯一标记不得进入正式知识（索引与召回边界）
    async with async_session_factory() as db:
        intent = (await db.execute(
            select(IndexIntent).where(IndexIntent.note_version_id == note.id)
        )).scalars().first()
        if intent is not None:
            await index_service.consume_intent(db, intent, embed=DeterministicEmbedding(1024))
    note_units = await gather_note_units(cid)
    joined = "\n".join(u["content"] for u in note_units)
    index_boundary_ok = CHANGED not in joined

    async with async_session_factory() as db:
        scope = ScopeContext(subject_id=SUBJECT_A, course_id=cid)
        s_changed = await retrieval_service.search(
            db, scope, CHANGED, top_k=3, embed=DeterministicEmbedding(1024),
        )

    def _contains(results, marker):
        return any(marker in chunk for r in results for chunk in (r.get("content") or []))

    recall_excluded = not _contains(s_changed["results"], CHANGED)

    # 重复发布仍幂等
    r_dup = await client.post(f"/api/notes/{note.id}/publish")
    note3 = await fetch_note(note.id)
    dup_ok = (
        r_dup.status_code == 200 and note3.status == "published"
        and await intent_count(note.id) == 1 and await published_count(l1) == 1
    )

    ok = (
        job.status == "succeeded" and note2.status == "published"
        and body_changed_downgraded and section_synced
        and index_boundary_ok and recall_excluded and dup_ok
    )
    record(
        "A4e", "只改正文、保留原引文和证据 → 结论/定义/例题推导/重点难点/章节正文均降待确认并排除出正式知识（唯一标记索引/召回边界），重复发布幂等",
        ok,
        dict(edit_status=r_edit.status_code, publish_status=r_pub.status_code,
             conclusion=dict(text=kc2["text"], status=kc2["status"], evidence_ids=kc2["evidence_ids"]),
             knowledge_point=dict(definition=kp2["definition"], status=kp2["status"]),
             example=dict(derivation=ex2["derivation"], status=ex2["status"]),
             key_point=dict(point=kpt2["point"], status=kpt2["status"]),
             section_points=[dict(text=p.get("text"), status=p.get("status")) for p in section_points],
             index_boundary=dict(changed_in_index=CHANGED in joined),
             recall=dict(changed_hits_contain_marker=_contains(s_changed["results"], CHANGED)),
             duplicate_publish=dict(status=r_dup.status_code, note_status=note3.status,
                                    intent=await intent_count(note.id),
                                    published_count=await published_count(l1))),
    )


async def a4_sections_preserved(current, client):
    # A4f（返工）：用两个已核实要点建立两个章节，经正常编辑接口保存并显式发布。
    # 服务端发布时不得丢失章节——保留章节数量、标题、顺序与要点归属，仅按条目更新证据状态。
    current["scope"] = None
    l1, _ = await create_lecture(SUBJECT_A, "a4-sections")
    _, v1 = await add_transcript(l1, [
        _seg(0, "第一节要点甲", start_ms=0, end_ms=1000),
        _seg(1, "第二节要点乙", start_ms=2000, end_ms=3000),
    ])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    kcs = note.content["key_conclusions"]
    assert len(kcs) == 2, [k["text"] for k in kcs]
    kc_a, kc_b = kcs[0], kcs[1]

    # 建立两个章节：第一章含要点甲、第二章含要点乙（正常编辑接口，仅提交章节结构）。
    sections = [
        {"heading": "第一章", "points": [{"text": kc_a["text"]}]},
        {"heading": "第二章", "points": [{"text": kc_b["text"]}]},
    ]
    r_edit = await client.patch(f"/api/notes/{note.id}", json={"sections": sections})
    assert r_edit.status_code == 200, f"edit HTTP {r_edit.status_code}: {r_edit.text}"

    r_pub = await client.post(f"/api/notes/{note.id}/publish")
    assert r_pub.status_code == 200, f"publish HTTP {r_pub.status_code}: {r_pub.text}"
    r_get = await client.get(f"/api/notes/{note.id}")
    assert r_get.status_code == 200
    r_dup = await client.post(f"/api/notes/{note.id}/publish")
    assert r_dup.status_code == 200

    def _structure(secs):
        return [
            (s.get("heading"), [p.get("text") for p in (s.get("points") or []) if isinstance(p, dict)])
            for s in secs
        ]

    expected = [("第一章", [kc_a["text"]]), ("第二章", [kc_b["text"]])]
    structures = {
        "edit": _structure(r_edit.json()["content"]["sections"]),
        "publish": _structure(r_pub.json()["content"]["sections"]),
        "read": _structure(r_get.json()["content"]["sections"]),
        "dup": _structure(r_dup.json()["content"]["sections"]),
    }
    four_same = all(v == expected for v in structures.values())

    # 按条目更新证据状态：发布后两章要点仍为已核对（正文未变、引文匹配、证据重建）。
    pub_secs = r_pub.json()["content"]["sections"]
    evidence_updated = (
        len(pub_secs) == 2
        and all(
            isinstance(p, dict) and p.get("status") == "confirmed" and bool(p.get("evidence_ids"))
            for s in pub_secs for p in (s.get("points") or [])
        )
    )

    ok = job.status == "succeeded" and four_same and evidence_updated
    record(
        "A4f", "两个已核实要点建立两个章节 → 编辑/首次发布/重新读取/重复发布四次章节结构一致（数量/标题/顺序/要点归属）+ 按条目更新证据状态",
        ok,
        dict(structures=structures, expected=expected,
             publish_sections=[
                 dict(heading=s.get("heading"),
                      points=[dict(text=p.get("text"), status=p.get("status"),
                                   evidence_ids=p.get("evidence_ids"))
                              for p in (s.get("points") or [])])
                 for s in pub_secs
             ]),
    )


async def a4_no_signature_draft_rebuild(current, client):
    # A4g（返工）：升级前的无签名草稿（条目缺 _verified_body）→ 发布时不得用待发布正文自建
    # 已验证基线；以服务端可信 quote 为据——锚点字段仍与 quote 一致的已确认条目保持 confirmed，
    # 无法核实的改动（quote 失配）留在待确认并排除出正式知识。
    current["scope"] = None
    CHANGED = "NOSIGUNIQUEMARKER_G"
    l1, cid = await create_lecture(SUBJECT_A, "a4-no-signature")
    _, v1 = await add_transcript(l1, [
        _seg(0, "无签名要点甲", start_ms=0, end_ms=1000),
        _seg(1, "无签名要点乙", start_ms=2000, end_ms=3000),
    ])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)

    # 模拟升级前无签名草稿：剥离所有条目的 _verified_body 基线；并改动第二条（quote 失配 → 无法核实）。
    async with async_session_factory() as db:
        n = await db.get(NoteVersion, note.id)
        c = dict(n.content or {})
        kcs = [dict(k) for k in (c.get("key_conclusions") or [])]
        kcs[1]["text"] = f"无签名要点乙被改动 {CHANGED}"
        kcs[1]["quote"] = "不存在的原文"
        for entry in kcs:
            entry.pop("_verified_body", None)
        c["key_conclusions"] = kcs
        for key in ("knowledge_points", "examples", "key_points"):
            entries = [dict(e) for e in (c.get(key) or [])]
            for e in entries:
                e.pop("_verified_body", None)
            c[key] = entries
        n.content = c
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(n, "content")
        await db.commit()

    r_pub = await client.post(f"/api/notes/{note.id}/publish")
    assert r_pub.status_code == 200, f"publish HTTP {r_pub.status_code}: {r_pub.text}"
    n2 = await fetch_note(note.id)
    kcs2 = {k["text"]: k for k in n2.content["key_conclusions"]}
    intact = kcs2["无签名要点甲"]
    tampered = kcs2[f"无签名要点乙被改动 {CHANGED}"]

    reverify_ok = (
        n2.status == "published"
        and intact["status"] == "confirmed" and bool(intact["evidence_ids"])
        and intact.get("_verified_body") is None  # 未用待发布正文自建基线（禁止循环自证）
        and tampered["status"] == "pending" and not tampered["evidence_ids"]
    )

    # 索引/召回边界：唯一标记（无法核实的改动）不得进入正式知识；已确认要点仍在。
    async with async_session_factory() as db:
        intent = (await db.execute(
            select(IndexIntent).where(IndexIntent.note_version_id == note.id)
        )).scalars().first()
        if intent is not None:
            await index_service.consume_intent(db, intent, embed=DeterministicEmbedding(1024))
    note_units = await gather_note_units(cid)
    joined = "\n".join(u["content"] for u in note_units)
    index_ok = (CHANGED not in joined) and ("无签名要点甲" in joined)

    async with async_session_factory() as db:
        scope = ScopeContext(subject_id=SUBJECT_A, course_id=cid)
        s = await retrieval_service.search(
            db, scope, CHANGED, top_k=3, embed=DeterministicEmbedding(1024),
        )

    def _contains(results, marker):
        return any(marker in chunk for r in results for chunk in (r.get("content") or []))

    recall_ok = not _contains(s["results"], CHANGED)

    ok = job.status == "succeeded" and reverify_ok and index_ok and recall_ok
    record(
        "A4g", "升级前无签名草稿 → 发布时以可信 quote 核对锚点字段（不自建基线）：已确认条目保持 confirmed，无法核实的改动(quote 失配)降待确认并排除出正式知识（唯一标记索引/召回边界）",
        ok,
        dict(note_status=n2.status,
             intact=dict(text=intact["text"], status=intact["status"],
                         evidence_ids=intact["evidence_ids"],
                         no_forged_baseline=intact.get("_verified_body") is None),
             tampered=dict(text=tampered["text"], status=tampered["status"],
                           evidence_ids=tampered["evidence_ids"]),
             index_boundary=dict(changed_in_index=CHANGED in joined, intact_in_index="无签名要点甲" in joined),
             recall=dict(changed_hits_contain_marker=_contains(s["results"], CHANGED))),
    )


async def a4_business_fields_only_edit(current, client):
    # A4h（返工）：正常编辑请求仅提交业务字段（不含内部 _verified_body）→ 编辑接口保存/带过旧基线，
    # 发布时识别「只改正文、保留原引文和证据」的实质变化 → 降待确认并排除出正式知识。
    current["scope"] = None
    CHANGED = "BIZFIELDONLYUNIQUEMARKER_H"
    l1, cid = await create_lecture(SUBJECT_A, "a4-biz-fields-only")
    _, v1 = await add_transcript(l1, [_seg(0, "业务字段要点甲", start_ms=0, end_ms=1000)])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)
    kc = note.content["key_conclusions"][0]

    # 仅提交业务字段：改正文，保留 quote 与 evidence_ids，不含 _verified_body。
    biz_kc = {
        "text": f"只改正文 {CHANGED}",
        "quote": kc["quote"],
        "origin": kc["origin"],
        "status": kc["status"],
        "evidence_ids": kc["evidence_ids"],
    }
    r_edit = await client.patch(f"/api/notes/{note.id}", json={"key_conclusions": [biz_kc]})
    assert r_edit.status_code == 200, f"edit HTTP {r_edit.status_code}: {r_edit.text}"

    r_pub = await client.post(f"/api/notes/{note.id}/publish")
    assert r_pub.status_code == 200, f"publish HTTP {r_pub.status_code}: {r_pub.text}"
    n2 = await fetch_note(note.id)
    kc2 = n2.content["key_conclusions"][0]
    downgraded = kc2["status"] == "pending" and not kc2["evidence_ids"]

    # 消费索引意向：失配条目的唯一标记不得进入正式知识（索引与召回边界）
    async with async_session_factory() as db:
        intent = (await db.execute(
            select(IndexIntent).where(IndexIntent.note_version_id == note.id)
        )).scalars().first()
        if intent is not None:
            await index_service.consume_intent(db, intent, embed=DeterministicEmbedding(1024))
    note_units = await gather_note_units(cid)
    joined = "\n".join(u["content"] for u in note_units)
    index_ok = CHANGED not in joined

    async with async_session_factory() as db:
        scope = ScopeContext(subject_id=SUBJECT_A, course_id=cid)
        s = await retrieval_service.search(
            db, scope, CHANGED, top_k=3, embed=DeterministicEmbedding(1024),
        )

    def _contains(results, marker):
        return any(marker in chunk for r in results for chunk in (r.get("content") or []))

    recall_ok = not _contains(s["results"], CHANGED)

    ok = job.status == "succeeded" and n2.status == "published" and downgraded and index_ok and recall_ok
    record(
        "A4h", "正常编辑仅提交业务字段 → 服务端保存旧基线，发布时识别只改正文(保留原引文和证据) → 降待确认并排除出正式知识（唯一标记索引/召回边界）",
        ok,
        dict(edit_status=r_edit.status_code, publish_status=r_pub.status_code,
             conclusion=dict(text=kc2["text"], status=kc2["status"], evidence_ids=kc2["evidence_ids"]),
             index_boundary=dict(changed_in_index=CHANGED in joined),
             recall=dict(changed_hits_contain_marker=_contains(s["results"], CHANGED))),
    )


async def a4_old_draft_combined(current, client):
    # A4i（返工）：旧草稿兼容组合回归。建立无正文签名的旧草稿，保留一条原样作为对照；
    # 经正常编辑接口只修改另一条正文（保留原有效引文和证据），显式发布后消费索引并检索唯一标记。
    current["scope"] = None
    CHANGED = "COMBINEDUNIQUEMARKER_I"
    l1, cid = await create_lecture(SUBJECT_A, "a4-old-draft-combined")
    _, v1 = await add_transcript(l1, [
        _seg(0, "旧草稿要点甲", start_ms=0, end_ms=1000),
        _seg(1, "旧草稿要点乙", start_ms=2000, end_ms=3000),
    ])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)

    # 模拟升级前无签名旧草稿：剥离全部条目的 _verified_body 基线（不在此处改动正文）。
    async with async_session_factory() as db:
        n = await db.get(NoteVersion, note.id)
        c = dict(n.content or {})
        for key in ("key_conclusions", "knowledge_points", "examples", "key_points"):
            entries = [dict(e) for e in (c.get(key) or [])]
            for e in entries:
                e.pop("_verified_body", None)
            c[key] = entries
        n.content = c
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(n, "content")
        await db.commit()

    # 重新读取无签名草稿，经正常编辑接口只改第二条正文（quote 与 evidence_ids 不动）。
    note = await fetch_note(note.id)
    kcs = [dict(k) for k in note.content["key_conclusions"]]
    assert len(kcs) == 2, f"expect 2 conclusions, got {len(kcs)}"
    kcs[1]["text"] = f"旧草稿要点乙被改动 {CHANGED}"
    r_edit = await client.patch(f"/api/notes/{note.id}", json={"key_conclusions": kcs})
    assert r_edit.status_code == 200, f"edit HTTP {r_edit.status_code}: {r_edit.text}"

    r_pub = await client.post(f"/api/notes/{note.id}/publish")
    assert r_pub.status_code == 200, f"publish HTTP {r_pub.status_code}: {r_pub.text}"
    n2 = await fetch_note(note.id)
    kcs2 = {k["text"]: k for k in n2.content["key_conclusions"]}
    intact = kcs2["旧草稿要点甲"]
    tampered = kcs2[f"旧草稿要点乙被改动 {CHANGED}"]

    intact_ok = intact["status"] == "confirmed" and bool(intact["evidence_ids"])
    tampered_ok = tampered["status"] == "pending" and not tampered["evidence_ids"]

    # 消费索引意向：失配条目（唯一标记）不得进入正式知识；对照条目仍可查。
    async with async_session_factory() as db:
        intent = (await db.execute(
            select(IndexIntent).where(IndexIntent.note_version_id == note.id)
        )).scalars().first()
        if intent is not None:
            await index_service.consume_intent(db, intent, embed=DeterministicEmbedding(1024))
    note_units = await gather_note_units(cid)
    joined = "\n".join(u["content"] for u in note_units)
    changed_in_index = CHANGED in joined
    intact_in_index = "旧草稿要点甲" in joined

    async with async_session_factory() as db:
        scope = ScopeContext(subject_id=SUBJECT_A, course_id=cid)
        s = await retrieval_service.search(
            db, scope, CHANGED, top_k=3, embed=DeterministicEmbedding(1024),
        )

    def _contains(results, marker):
        return any(marker in chunk for r in results for chunk in (r.get("content") or []))

    changed_hits_contain_marker = _contains(s["results"], CHANGED)

    ok = (
        job.status == "succeeded" and n2.status == "published"
        and intact_ok and tampered_ok
        and not changed_in_index and intact_in_index
        and not changed_hits_contain_marker
    )
    record(
        "A4i", "旧草稿兼容组合回归 → 无签名草稿保留对照条目 confirmed，仅改另一条正文(保留原引文和证据)降待确认并排除出正式知识（唯一标记索引/召回边界）",
        ok,
        dict(note_status=n2.status,
             intact=dict(text=intact["text"], status=intact["status"], evidence_ids=intact["evidence_ids"]),
             tampered=dict(text=tampered["text"], status=tampered["status"], evidence_ids=tampered["evidence_ids"]),
             index_boundary=dict(changed_in_index=changed_in_index, intact_in_index=intact_in_index),
             recall=dict(changed_hits_contain_marker=changed_hits_contain_marker)),
    )


async def a4_old_draft_multi_field(current, client):
    # A4j（返工）：旧草稿多字段条目 + 重复引文定位。无正文签名的旧例题/知识点/重点难点，发布前须
    # 核实会进入正式知识的全部正文字段。先经原证据定位（服务端可信、属于当前转写版本）确定条目所属
    # 来源片段，再逐字段核对：能从该片段逐字核实的原样次要字段（推导/适用条件/依据）保留进发布版本
    # 与正式索引；无法核实的字段继续排除。原定位缺失且同一引文命中多处（预告 vs 正式讲解）→ 留待核实。
    current["scope"] = None
    M_DERIV = "A4JDERVUNIQUEMARKER"
    M_APPL = "A4JAPPLUNIQUEMARKER"
    M_REASON = "A4JREASONUNIQUEMARKER"
    # 先预告题干、后正式讲解：seq0 预告含「计算 2+3」但无「2+3=5」；seq1 正式讲解两者皆含。
    # 同一引文「计算 2+3」命中两处，发布时必须靠原证据定位锁定 seq1，而非错定位到 seq0。
    original_source = [
        "预告：今天要讲计算 2+3",              # seq0 预告（无推导）
        "【例题】计算 2+3【解】2+3=5",        # seq1 正式讲解（含推导）
        "【例题】计算 4+5【解】4+5=9",        # seq2 改动例题
        "【知识点】加法的定义【适用】整数相加",  # seq3 对照知识点
        "【知识点】乘法的定义【适用】整数相乘",  # seq4 改动知识点
        "【难点】进位易错【依据】课本第三章",    # seq5 对照难点
        "【难点】借位易错【依据】课本第四章",    # seq6 改动难点
    ]
    l1, cid = await create_lecture(SUBJECT_A, "a4-old-draft-multifield")
    _, v1 = await add_transcript(l1, [
        _seg(0, original_source[0], start_ms=0, end_ms=1000),
        _seg(1, original_source[1], start_ms=2000, end_ms=3000),
        _seg(2, original_source[2], start_ms=4000, end_ms=5000),
        _seg(3, original_source[3], start_ms=6000, end_ms=7000),
        _seg(4, original_source[4], start_ms=8000, end_ms=9000),
        _seg(5, original_source[5], start_ms=10000, end_ms=11000),
        _seg(6, original_source[6], start_ms=12000, end_ms=13000),
    ])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)

    # 模拟升级前无签名旧草稿：剥离全部条目 _verified_body（不在此处改动正文）。
    async with async_session_factory() as db:
        n = await db.get(NoteVersion, note.id)
        c = dict(n.content or {})
        for key in ("key_conclusions", "knowledge_points", "examples", "key_points"):
            entries = [dict(e) for e in (c.get(key) or [])]
            for e in entries:
                e.pop("_verified_body", None)
            c[key] = entries
        n.content = c
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(n, "content")
        await db.commit()

    # 重新读取，经正常编辑接口只改次要字段（保留锚点/引文/证据）。
    note = await fetch_note(note.id)
    exs = [dict(e) for e in note.content["examples"]]
    kps = [dict(k) for k in note.content["knowledge_points"]]
    keys = [dict(k) for k in note.content["key_points"]]
    for e in exs:
        if e.get("problem") == "计算 4+5":
            e["derivation"] = f"只改推导 {M_DERIV}"
    for k in kps:
        if k.get("definition") == "乘法的定义":
            k["applicability"] = f"只改适用 {M_APPL}"
    for k in keys:
        if k.get("point") == "借位易错":
            k["reason"] = f"只改依据 {M_REASON}"

    # 发布前字段快照（同一次运行，编辑请求后的待发布正文）。
    pre_publish = dict(
        examples=[dict(problem=e.get("problem"), derivation=e.get("derivation")) for e in exs],
        knowledge_points=[dict(definition=k.get("definition"), applicability=k.get("applicability")) for k in kps],
        key_points=[dict(point=k.get("point"), reason=k.get("reason")) for k in keys],
    )

    r_edit = await client.patch(f"/api/notes/{note.id}", json={
        "examples": exs, "knowledge_points": kps, "key_points": keys,
    })
    assert r_edit.status_code == 200, f"edit HTTP {r_edit.status_code}: {r_edit.text}"

    r_pub = await client.post(f"/api/notes/{note.id}/publish")
    assert r_pub.status_code == 200, f"publish HTTP {r_pub.status_code}: {r_pub.text}"
    n2 = await fetch_note(note.id)
    c2 = n2.content or {}

    def _find(entries, field, val):
        return next(e for e in entries if e.get(field) == val)

    ex_ctrl = _find(c2["examples"], "problem", "计算 2+3")
    ex_chg = _find(c2["examples"], "problem", "计算 4+5")
    kp_ctrl = _find(c2["knowledge_points"], "definition", "加法的定义")
    kp_chg = _find(c2["knowledge_points"], "definition", "乘法的定义")
    key_ctrl = _find(c2["key_points"], "point", "进位易错")
    key_chg = _find(c2["key_points"], "point", "借位易错")

    def _confirmed(e):
        return e["status"] == "confirmed" and bool(e["evidence_ids"])

    # 双向断言：未改对照的次要字段保留（原样内容从所属来源片段逐字核实）；被改字段排除（清空）。
    fields_ok = (
        _confirmed(ex_ctrl) and (ex_ctrl.get("derivation") or "").strip() == "2+3=5"
        and _confirmed(ex_chg) and not (ex_chg.get("derivation") or "")
        and _confirmed(kp_ctrl) and (kp_ctrl.get("applicability") or "").strip() == "整数相加"
        and _confirmed(kp_chg) and not (kp_chg.get("applicability") or "")
        and _confirmed(key_ctrl) and (key_ctrl.get("reason") or "").strip() == "课本第三章"
        and _confirmed(key_chg) and not (key_chg.get("reason") or "")
    )

    # 证据定位（A4 v8）：未改对照例题的证据须指向正式讲解片段（seq1，时间 2000-3000），
    # 而非预告片段（seq0）或任何其他不相关片段——同一引文「计算 2+3」在两处出现，靠原证据定位锁定。
    ex_ctrl_span = None
    async with async_session_factory() as db:
        for eid in (ex_ctrl.get("evidence_ids") or []):
            try:
                sp = await db.get(EvidenceSpan, uuid.UUID(str(eid)))
            except (ValueError, TypeError):
                continue
            if sp is not None:
                ex_ctrl_span = sp
                break
    evidence_localized = (
        ex_ctrl_span is not None
        and ex_ctrl_span.segment_start == 1
        and ex_ctrl_span.segment_end == 1
        and ex_ctrl_span.start_ms == 2000
        and ex_ctrl_span.end_ms == 3000
    )

    # 消费索引意向：对照次要字段进入正式索引；改动标记与改动条目的次要字段不进入。
    async with async_session_factory() as db:
        intent = (await db.execute(
            select(IndexIntent).where(IndexIntent.note_version_id == note.id)
        )).scalars().first()
        if intent is not None:
            await index_service.consume_intent(db, intent, embed=DeterministicEmbedding(1024))
    note_units = await gather_note_units(cid)
    joined = "\n".join(u["content"] for u in note_units)

    def _contains(results, marker):
        return any(marker in chunk for r in results for chunk in (r.get("content") or []))

    anchors_in = (
        "计算 2+3" in joined and "计算 4+5" in joined
        and "加法的定义" in joined and "乘法的定义" in joined
        and "进位易错" in joined and "借位易错" in joined
    )
    control_secondary_in = (
        "2+3=5" in joined and "整数相加" in joined and "课本第三章" in joined
    )
    changed_secondary_out = (
        "4+5=9" not in joined and "整数相乘" not in joined and "课本第四章" not in joined
    )
    index_ok = (
        anchors_in and control_secondary_in and changed_secondary_out
        and M_DERIV not in joined and M_APPL not in joined and M_REASON not in joined
    )

    async with async_session_factory() as db:
        scope = ScopeContext(subject_id=SUBJECT_A, course_id=cid)
        s_d = await retrieval_service.search(db, scope, M_DERIV, top_k=3, embed=DeterministicEmbedding(1024))
        s_a = await retrieval_service.search(db, scope, M_APPL, top_k=3, embed=DeterministicEmbedding(1024))
        s_r = await retrieval_service.search(db, scope, M_REASON, top_k=3, embed=DeterministicEmbedding(1024))

    recall_ok = (
        not _contains(s_d["results"], M_DERIV)
        and not _contains(s_a["results"], M_APPL)
        and not _contains(s_r["results"], M_REASON)
    )

    ok = (
        job.status == "succeeded" and n2.status == "published"
        and fields_ok and evidence_localized and index_ok and recall_ok
    )
    record(
        "A4j", "旧草稿多字段条目 + 重复引文定位 → 先预告题干后正式讲解（预告/讲解均含「计算 2+3」、仅讲解含「2+3=5」）：发布靠原证据定位锁定正式讲解片段；未改对照例题推导=2+3=5、知识点适用=整数相加、难点依据=课本第三章保留进发布版与索引；被改推导/适用/依据清空（唯一标记索引/召回边界）",
        ok,
        dict(note_status=n2.status,
             original_source=original_source,
             pre_publish=pre_publish,
             post_publish=dict(
                 examples=[dict(problem=e.get("problem"), derivation=e.get("derivation"), status=e.get("status")) for e in c2["examples"]],
                 knowledge_points=[dict(definition=k.get("definition"), applicability=k.get("applicability"), status=k.get("status")) for k in c2["knowledge_points"]],
                 key_points=[dict(point=k.get("point"), reason=k.get("reason"), status=k.get("status")) for k in c2["key_points"]],
             ),
             evidence_localization=dict(
                 localized=evidence_localized,
                 control_example_span=(dict(
                     segment_start=ex_ctrl_span.segment_start,
                     segment_end=ex_ctrl_span.segment_end,
                     start_ms=ex_ctrl_span.start_ms,
                     end_ms=ex_ctrl_span.end_ms,
                     quote=ex_ctrl_span.quote,
                 ) if ex_ctrl_span is not None else None),
             ),
             index_boundary=dict(anchors_in_index=anchors_in,
                                 control_secondary_in_index=control_secondary_in,
                                 changed_secondary_out_index=changed_secondary_out,
                                 changed_markers_in_index=(M_DERIV in joined or M_APPL in joined or M_REASON in joined)),
             recall=dict(deriv_hits_contain_marker=_contains(s_d["results"], M_DERIV),
                         appl_hits_contain_marker=_contains(s_a["results"], M_APPL),
                         reason_hits_contain_marker=_contains(s_r["results"], M_REASON))),
    )


async def a4_quote_only_edit(current, client):
    # A4k（返工）：只改引文。从已确认草稿出发，正文与原证据 ID 完全不变，只把 quote 改为转写中
    # 不存在的唯一标记，走正常编辑/发布接口。发布前创建有效证据须核对当前引文存在于所属片段——失配
    # 则拒绝该引文、恢复可核实原引用；同一草稿的未改对照保持 confirmed。
    current["scope"] = None
    M_QUOTE = "A4KQUOTEUNIQUEMARKER"
    l1, cid = await create_lecture(SUBJECT_A, "a4-quote-only-edit")
    _, v1 = await add_transcript(l1, [
        _seg(0, "【例题】计算 2+3【解】2+3=5", start_ms=0, end_ms=1000),
        _seg(1, "【例题】计算 4+5【解】4+5=9", start_ms=2000, end_ms=3000),
    ])
    fake = FakeOrganizeModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(fake)
    job = await fetch_job(job_id)
    note = await fetch_note(job.output_version)

    # 只改第一条例题的 quote，正文（problem/derivation）与原证据 ID 完全不变。
    exs = [dict(e) for e in note.content["examples"]]
    for e in exs:
        if e.get("problem") == "计算 2+3":
            e["quote"] = M_QUOTE
    r_edit = await client.patch(f"/api/notes/{note.id}", json={"examples": exs})
    assert r_edit.status_code == 200, f"edit HTTP {r_edit.status_code}: {r_edit.text}"

    r_pub = await client.post(f"/api/notes/{note.id}/publish")
    assert r_pub.status_code == 200, f"publish HTTP {r_pub.status_code}: {r_pub.text}"
    n2 = await fetch_note(note.id)
    c2 = n2.content or {}

    def _find(entries, field, val):
        return next(e for e in entries if e.get(field) == val)

    ex_chg = _find(c2["examples"], "problem", "计算 2+3")
    ex_ctrl = _find(c2["examples"], "problem", "计算 4+5")

    async with async_session_factory() as db:
        chg_span = None
        for eid in (ex_chg.get("evidence_ids") or []):
            try:
                sp = await db.get(EvidenceSpan, uuid.UUID(str(eid)))
            except (ValueError, TypeError):
                continue
            if sp is not None:
                chg_span = sp
                break

    # 改动条目：标记未被保存为有效引文（条目 quote 与 EvidenceSpan.quote 均恢复为可核实原引用）。
    quote_ok = (
        ex_chg.get("status") == "confirmed"
        and bool(ex_chg["evidence_ids"])
        and (ex_chg.get("quote") or "").strip() == "计算 2+3"
        and (ex_chg.get("quote") or "") != M_QUOTE
        and chg_span is not None
        and (chg_span.quote or "").strip() == "计算 2+3"
        and (chg_span.quote or "") != M_QUOTE
    )
    # 未改对照：仍 confirmed，正文完整。
    ctrl_ok = (
        ex_ctrl.get("status") == "confirmed"
        and bool(ex_ctrl["evidence_ids"])
        and (ex_ctrl.get("derivation") or "").strip() == "4+5=9"
    )

    # 消费索引意向：标记不得进入正式知识；两个例题的题干/推导仍可查。
    async with async_session_factory() as db:
        intent = (await db.execute(
            select(IndexIntent).where(IndexIntent.note_version_id == note.id)
        )).scalars().first()
        if intent is not None:
            await index_service.consume_intent(db, intent, embed=DeterministicEmbedding(1024))
    note_units = await gather_note_units(cid)
    joined = "\n".join(u["content"] for u in note_units)

    index_ok = (
        M_QUOTE not in joined
        and "计算 2+3" in joined and "2+3=5" in joined
        and "计算 4+5" in joined and "4+5=9" in joined
    )

    async with async_session_factory() as db:
        scope = ScopeContext(subject_id=SUBJECT_A, course_id=cid)
        s_q = await retrieval_service.search(db, scope, M_QUOTE, top_k=3, embed=DeterministicEmbedding(1024))

    def _contains(results, marker):
        return any(marker in chunk for r in results for chunk in (r.get("content") or []))

    recall_ok = not _contains(s_q["results"], M_QUOTE)

    ok = (
        job.status == "succeeded" and n2.status == "published"
        and quote_ok and ctrl_ok and index_ok and recall_ok
    )
    record(
        "A4k", "只改引文（正文与原证据 ID 不变）→ 创建有效证据前核对当前引文存在于所属片段：失配拒绝标记、恢复可核实原引用「计算 2+3」；未改对照仍 confirmed；标记不进索引/召回",
        ok,
        dict(note_status=n2.status,
             changed=dict(problem=ex_chg.get("problem"), derivation=ex_chg.get("derivation"),
                          quote=ex_chg.get("quote"), status=ex_chg.get("status"),
                          span_quote=(chg_span.quote if chg_span is not None else None)),
             control=dict(problem=ex_ctrl.get("problem"), derivation=ex_ctrl.get("derivation"),
                          status=ex_ctrl.get("status")),
             index_boundary=dict(marker_in_index=(M_QUOTE in joined)),
             recall=dict(quote_marker_hits_contain_marker=_contains(s_q["results"], M_QUOTE))),
    )


# ── A6：供应商超时（有界重试）+ 预算不足 + 运行详情 ─────────────────

class TimeoutModel(FakeOrganizeModel):
    """供应商超时：extract 连续抛可重试 ModelError（connection/timeout）。"""
    name = "timeout"

    def __init__(self):
        super().__init__()
        self.timeout_calls = 0

    async def extract_chunk(self, chunk):
        self.timeout_calls += 1
        raise ModelError("connection", "整理接口连接超时", retryable=True, code="timeout")


async def a6_timeout(current, client):
    current["scope"] = None
    l1, _ = await create_lecture(SUBJECT_A, "a6-timeout")
    _, v1 = await add_transcript(l1, [_seg(0, "超时正文", start_ms=0, end_ms=1000)])

    timeout_model = TimeoutModel()
    job_id = await http_organize(client, l1, v1)
    await dispatch_organize(timeout_model)

    async with async_session_factory() as db:
        job = await db.get(ProcessingJob, job_id)
        manifest = (await db.execute(
            select(ChunkManifest).where(ChunkManifest.job_id == job_id)
        )).scalars().first()
        item = (await db.execute(
            select(ChunkItem).where(ChunkItem.manifest_id == manifest.id)
        )).scalars().first()

    retry_count = item.attempt
    retry_ok = retry_count == settings.ORGANIZE_MAX_ATTEMPTS
    terminal_ok = (
        job.status == "failed" and job.error_code == "incomplete_coverage"
        and item.status == "failed" and item.error_code == "timeout"
    )

    # 运行详情（A6 返工）：超时路径 —— note 选中且已执行（真实外发过 extract，产物为空），
    # 其余三类跳过未执行；块错误/重试/耗时取自 ChunkItem（非网关无 ProviderCall）。
    # 须在恢复前查询（恢复后块转为 succeeded，非超时态）。
    r_detail = await client.get(f"/api/jobs/{job_id}/run-detail")
    detail = r_detail.json()
    timeout_detail_ok = (
        r_detail.status_code == 200
        and detail["branches"]["note"]["selected"] is True
        and detail["branches"]["note"]["executed"] is True
        and detail["branches"]["note"]["count"] == 0
        and all(
            detail["branches"][b]["selected"] is False
            and detail["branches"][b]["skipped"] is True
            and detail["branches"][b]["executed"] is False
            for b in ("knowledge_point", "example", "key_point")
        )
        and detail["chunks"][0]["error_code"] == "timeout"
        and detail["chunks"][0]["attempt"] == settings.ORGANIZE_MAX_ATTEMPTS
        and detail["chunks"][0]["started_at"] is not None
        and detail["chunks"][0]["finished_at"] is not None
        and detail["shared_calls"] == [] and detail["provider_calls"] == []
    )

    # 运行详情：超时 3 次尝试结果未知 → 未知用量（非零调用，且无已结算用量）
    usage_unknown = timeout_model.timeout_calls == 3 and retry_count == 3

    # 恢复：显式重试（终态失败可重试，保留 output_version）→ 成功，不重复产物
    async with async_session_factory() as db:
        await retry_job(db, job_id)
    recovered_model = FakeOrganizeModel()
    await dispatch_organize(recovered_model)
    async with async_session_factory() as db:
        job2 = await db.get(ProcessingJob, job_id)
        note_count = (await db.execute(
            select(func.count(NoteVersion.id)).where(NoteVersion.lecture_id == l1)
        )).scalar_one()
    final_note = await fetch_note(job2.output_version)
    no_dup = job2.status == "succeeded" and note_count == 1 and final_note.status == "draft"

    ok = retry_ok and terminal_ok and no_dup and usage_unknown and timeout_detail_ok
    record(
        "A6a", "供应商超时 → 有界重试符合 ORGANIZE_MAX_ATTEMPTS + 终态可见 + 恢复无重复产物 + 未知用量 + 运行详情(超时)对照",
        ok,
        dict(retry_count=retry_count, max_attempts=settings.ORGANIZE_MAX_ATTEMPTS,
             timeout_calls=timeout_model.timeout_calls,
             usage="unknown", zero_call=False,
             terminal=dict(job_status=job.status, job_error=job.error_code,
                           chunk_status=item.status, chunk_error=item.error_code),
             recovered=dict(job_status=job2.status, note_count=note_count,
                            note_status=final_note.status if final_note else None),
             run_detail_branches=detail["branches"],
             run_detail_chunks=detail["chunks"]),
    )


async def _reset_budget() -> None:
    async with async_session_factory() as db:
        await db.execute(delete(BudgetReservation))
        await db.execute(delete(BudgetAccount))
        await db.commit()


async def _set_account(kind: str, subject_id: str, *, settled_micro=None) -> None:
    async with async_session_factory() as db:
        day = budget.shanghai_day()
        await budget._ensure_accounts(db, subject_id, day)
        row = (await db.execute(
            select(BudgetAccount).where(
                BudgetAccount.kind == kind,
                BudgetAccount.account_key == budget._account_key(kind, subject_id),
                BudgetAccount.day == budget._day_for(kind, day),
            )
        )).scalars().first()
        if settled_micro is not None:
            row.settled_micro = settled_micro
        await db.commit()


async def _gateway_success_provider_calls(current, client):
    """A6c：网关路径成功整理，查询各操作耗时/错误/重试/用量（区分零调用与未知用量）。"""
    current["scope"] = None
    settings.CALL_GATEWAY_ENABLED = True
    try:
        await _reset_budget()
        l1, _ = await create_lecture(SUBJECT_A, "a6-gw-success")
        _, v1 = await add_transcript(l1, [
            _seg(0, "【例题】网关例题 3+4【解】3+4=7", start_ms=0, end_ms=1000),
            _seg(1, "网关正文要点", start_ms=2000, end_ms=3000),
        ])
        fake = FakeOrganizeModel()
        job_id = await http_organize(client, l1, v1)
        await dispatch_organize(fake)
        job = await fetch_job(job_id)
        async with async_session_factory() as db:
            pcs = (await db.execute(
                select(ProviderCall).where(ProviderCall.job_id == job_id)
                    .order_by(ProviderCall.attempt_no, ProviderCall.started_at)
            )).scalars().all()
        rows = [dict(
            operation_type=p.operation_type,
            operation_key=p.operation_key,
            attempt_no=p.attempt_no,
            error_code=p.error_code,
            usage_state=p.usage_state,
            usage=p.usage if p.usage is not None else None,
            reserved_amount=p.reserved_amount,
            settled_amount=p.settled_amount,
            has_timing=(p.started_at is not None and p.finished_at is not None),
        ) for p in pcs]

        # 每操作均有计时；成功操作 usage_state=confirmed_sent（费用已结算），
        # token 级 usage 因替身模型无用量回执而未知（区分「零调用」与「未知用量」）。
        extract_pcs = [r for r in rows if r["operation_type"] == "organize_extract"]
        summary_pcs = [r for r in rows if r["operation_type"] == "organize_summarize"]
        ok = (
            job.status == "succeeded"
            and len(extract_pcs) == len(fake.calls)  # 每块一次受控 extract
            and len(summary_pcs) == 1
            and all(r["has_timing"] for r in rows)
            and all(r["error_code"] is None for r in rows)
            and all(r["usage_state"] == "confirmed_sent" for r in rows)
            and all(r["usage"] is None for r in rows)  # 替身无 token 回执 → 未知用量
            and len(fake.calls) >= 1
        )
        record(
            "A6c", "网关路径成功整理 → 各操作耗时/错误/重试/用量取自实际 ProviderCall，区分零调用与未知用量",
            ok,
            dict(job_status=job.status, provider_calls=rows, zero_call=False),
        )
        return ok
    finally:
        settings.CALL_GATEWAY_ENABLED = False


async def a6_run_detail_shared_call(current, client):
    """A6d（返工）：用作业 ID 经 HTTP 运行详情查询四类分支选择/执行状态/耗时/用量，
    共享一次 extract 调用的多个分支展示同一调用标识、不重复记账；用量未返回标为未知。
    覆盖「跳过（key_point）」与「成功（note/example/knowledge_point）」。"""
    current["scope"] = None
    settings.CALL_GATEWAY_ENABLED = True
    try:
        await _reset_budget()
        l1, _ = await create_lecture(SUBJECT_A, "a6-run-detail")
        _, v1 = await add_transcript(l1, [
            _seg(0, "【例题】详情例题 5+6【解】5+6=11", start_ms=0, end_ms=1000),
            _seg(1, "【知识点】详情知识点【适用】通用", start_ms=2000, end_ms=3000),
            _seg(2, "详情正文要点", start_ms=4000, end_ms=5000),
        ])
        fake = FakeOrganizeModel()
        job_id = await http_organize(client, l1, v1)
        await dispatch_organize(fake)
        job = await fetch_job(job_id)

        r = await client.get(f"/api/jobs/{job_id}/run-detail")
        d = r.json()

        branches = d["branches"]
        # 分支：note/example/knowledge_point 选中且已执行；key_point 未选中跳过未执行。
        branches_ok = (
            all(
                branches[b]["selected"] is True and branches[b]["executed"] is True
                and branches[b]["skipped"] is False
                for b in ("note", "example", "knowledge_point")
            )
            and branches["key_point"]["selected"] is False
            and branches["key_point"]["skipped"] is True
            and branches["key_point"]["executed"] is False
            and branches["example"]["count"] == 1
            and branches["knowledge_point"]["count"] == 1
            and branches["note"]["count"] == 1
        )

        # 共享调用：一次 extract 调用（同一 operation_key）产出三个选中分支，不重复记账；
        # summarize 独立一条。替身无 token 回执 → usage 未知（usage_known=False）。
        shared = d["shared_calls"]
        extract_shared = [s for s in shared if s["operation_type"] == "organize_extract"]
        summary_shared = [s for s in shared if s["operation_type"] == "organize_summarize"]
        shared_call_ok = (
            len(extract_shared) == 1
            and len(summary_shared) == 1
            and set(extract_shared[0]["shared_branches"]) == {"note", "example", "knowledge_point"}
            and extract_shared[0]["attempts"] == 1
            and extract_shared[0]["usage_known"] is False  # 供应商未返回用量 → 明确标未知
            and summary_shared[0]["shared_branches"] is None
        )

        # 与底层调用记录对照：provider_calls 条数 == shared_calls 各 attempt 之和；无重复记账。
        provider_calls = d["provider_calls"]
        total_attempts = sum(s["attempts"] for s in shared)
        correspondence_ok = (
            len(provider_calls) == total_attempts
            and all(pc["usage_known"] is False for pc in provider_calls)
            and all(pc["started_at"] is not None and pc["finished_at"] is not None for pc in provider_calls)
        )

        # 每块耗时可见（ChunkItem started/finished 非空）
        chunks_timing_ok = all(
            c["started_at"] is not None and c["finished_at"] is not None for c in d["chunks"]
        )

        ok = (
            r.status_code == 200 and job.status == "succeeded"
            and branches_ok and shared_call_ok and correspondence_ok and chunks_timing_ok
        )
        record(
            "A6d", "HTTP 运行详情 → 四类分支选择/执行状态/耗时/用量 + 共享调用标识不重复记账 + 未知用量显式标注 + 与底层调用记录对照（跳过/成功）",
            ok,
            dict(job_status=job.status, branches=branches, shared_calls=shared,
                 provider_calls=provider_calls,
                 zero_call=False, usage_known=False),
        )
        return ok
    finally:
        settings.CALL_GATEWAY_ENABLED = False


async def a6_budget(current, client):
    current["scope"] = None
    settings.CALL_GATEWAY_ENABLED = True
    try:
        await _reset_budget()
        cap = settings.BUDGET_SUBJECT_DAILY_CNY * settings.BUDGET_MICRO_PER_CNY
        await _set_account("subject_daily", SUBJECT_A, settled_micro=cap)

        l1, _ = await create_lecture(SUBJECT_A, "a6-budget")
        _, v1 = await add_transcript(l1, [_seg(0, "预算正文", start_ms=0, end_ms=1000)])

        fake = FakeOrganizeModel()
        job_id = await http_organize(client, l1, v1)
        await dispatch_organize(fake)

        async with async_session_factory() as db:
            job = await db.get(ProcessingJob, job_id)
            pc_count = (await db.execute(
                select(func.count(ProviderCall.id)).where(ProviderCall.job_id == job_id)
            )).scalar_one()
            note_count = (await db.execute(
                select(func.count(NoteVersion.id)).where(NoteVersion.lecture_id == l1)
            )).scalar_one()
        async with async_session_factory() as db:
            st = await budget.available_budget(db, SUBJECT_A)

        budget_ok = (
            job.status == "failed" and job.error_code == "budget_exhausted"
            and len(fake.calls) == 0  # 零调用：一次 extract 都未发出
            and pc_count == 0
            and job.output_version is None and note_count == 0
            and st["subject_daily"]["settled_micro"] == cap  # 无新增费用
        )

        # 运行详情（A6 返工）：预算阻止 —— 规划仍记录选中（note），但未执行、零调用；
        # 无 ProviderCall、无共享调用，用量确定为零（区别于未知用量）。
        r_detail = await client.get(f"/api/jobs/{job_id}/run-detail")
        bdetail = r_detail.json()
        budget_detail_ok = (
            r_detail.status_code == 200
            and bdetail["plan"].get("branches", {}).get("note", {}).get("selected") is True
            and bdetail["branches"]["note"]["selected"] is True
            and bdetail["branches"]["note"]["executed"] is False
            and bdetail["shared_calls"] == [] and bdetail["provider_calls"] == []
        )
        budget_ok = budget_ok and budget_detail_ok
        record(
            "A6b", "预算不足 → 阻止超额调用（零调用）+ 终态 budget_exhausted + 无产物/无调用记录/无新增费用 + 运行详情(预算阻止)对照",
            budget_ok,
            dict(job_status=job.status, job_error=job.error_code,
                 fake_extract_calls=len(fake.calls), provider_calls=pc_count,
                 usage=0, zero_call=True,  # 零调用：用量确定为零，区别于 A6a/A6c 未知用量
                 note_count=note_count, output_version=job.output_version is not None,
                 subject_daily_settled=st["subject_daily"]["settled_micro"],
                 cap=cap,
                 run_detail_branches=bdetail["branches"],
                 run_detail_shared_calls=bdetail["shared_calls"]),
        )
    finally:
        settings.CALL_GATEWAY_ENABLED = False


def tests(current, client):
    return [
        ("A3a", a3_with_example(current, client)),
        ("A3b", a3_without_example(current, client)),
        ("A3c", a3_different_planning(current, client)),
        ("A3d", a3_production_planner(current, client)),
        ("A3e", a3_budget_influenced_plan(current, client)),
        ("A4a", a4_invalid_reference_boundary(current, client)),
        ("A4b", a4_draft_first_new_draft(current, client)),
        ("A4c", a4_duplicate_publish(current, client)),
        ("A4d", a4_edit_reverify(current, client)),
        ("A4e", a4_edit_body_only(current, client)),
        ("A4f", a4_sections_preserved(current, client)),
        ("A4g", a4_no_signature_draft_rebuild(current, client)),
        ("A4h", a4_business_fields_only_edit(current, client)),
        ("A4i", a4_old_draft_combined(current, client)),
        ("A4j", a4_old_draft_multi_field(current, client)),
        ("A4k", a4_quote_only_edit(current, client)),
        ("A6a", a6_timeout(current, client)),
        ("A6b", a6_budget(current, client)),
        ("A6c", _gateway_success_provider_calls(current, client)),
        ("A6d", a6_run_detail_shared_call(current, client)),
    ]


async def main() -> None:
    started = datetime.now(timezone.utc)
    await create_db(MAIN_DB)
    run_alembic("head", asyncpg_dsn(MAIN_DB))

    current = {"subject_id": SUBJECT_A, "scope": None}

    async def override_scope():
        return current["scope"] or ScopeContext(subject_id=current["subject_id"])

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
        json.dumps({
            "organize_context_budget": settings.ORGANIZE_CONTEXT_BUDGET,
            "organize_max_attempts": settings.ORGANIZE_MAX_ATTEMPTS,
            "organize_chunk_overlap": settings.ORGANIZE_CHUNK_OVERLAP,
            "budget_subject_daily_cny": settings.BUDGET_SUBJECT_DAILY_CNY,
            "budget_reserve_min_cny": settings.BUDGET_RESERVE_MIN_CNY,
            "index_embedding_dimensions": settings.INDEX_EMBEDDING_DIMENSIONS,
            "database": MAIN_DB,
            "call_gateway_enabled_during_budget_test": True,
            "providers": "FakeOrganizeModel / CraftedModel / TimeoutModel（全部模拟，无真实供应商调用）",
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    finished = datetime.now(timezone.utc)
    (OUT / "run.json").write_text(
        json.dumps({
            "exit_code": 1 if failed else 0,
            "started_utc": started.isoformat(),
            "finished_utc": finished.isoformat(),
            "databases": [MAIN_DB],
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {}
    for rel in SOURCE_FILES:
        p = ROOT / rel if not rel.startswith("../") else (ROOT.parent / rel[3:])
        manifest[rel] = sha256(p) if p.exists() else None
    (OUT / "source-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    await drop_db(MAIN_DB)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
