"""reset-v1 阶段 4「问答 / 检索闭环」验收（A5 / R5）独立验证。

对应 docs/classroom-agent/reset-v1/03-acceptance-v1.1.md：
- R5 问答：支持本课时 / 本课程提问；资料路检索转写、课件、已发布笔记，结构化路检索
  知识点及例题；融合后流式回答，引用能定位来源，证据不足时明确说明。
- A5 / R5：分别问课件细节、结构化例题、资料外问题和另一个课时的标记词；切换课时与课程范围。
  保留两路召回与融合记录、流式响应；引用对应原文；课时范围排除兄弟课时，课程范围可查
  该课其他课时，当前课程范围排除其他课程的标记词和材料，资料外问题提示依据不足。

本脚本通过实际课堂问答 SSE 入口（POST /api/chat/classroom/stream）端到端验证：
chat_service.classroom_chat_stream → retrieval_service.search（向量 + 词法两路 → RRF 融合
→ 同源去重 → 规则重排）→ 流式「sources / token / done」事件。检索与索引均用确定性
embedding（DeterministicEmbedding），不调用收费模型。

运行（backend 目录）：
  .venv/Scripts/python.exe scripts/verify_resetv1_phase4.py
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
PREFIX = "yuan_resetv1_p4_" + uuid.uuid4().hex[:10] + "_"
MAIN_DB = PREFIX + "main"

# 在导入会创建引擎的模块前，把引擎指向隔离测试库，绝不触碰生产库。
settings.DATABASE_URL = BASE.replace("postgresql://", "postgresql+asyncpg://") + "/" + MAIN_DB
os.environ["DATABASE_URL"] = settings.DATABASE_URL
# 课堂问答与索引/检索统一走确定性 embedding，避免真实供应商调用且使向量/词法两路可验证。
settings.INDEX_EMBEDDING_PROVIDER = "deterministic"

from app.api.deps import get_scope
from app.db.session import async_session_factory
from app.main import app
from app.models.audio_asset import AudioAsset
from app.models.course import Course
from app.models.meeting import Meeting
from app.models.note_version import NoteVersion
from app.models.parsed_block import ParsedBlock
from app.models.source_document import SourceDocument, SourceDocumentVersion
from app.models.transcript_version import TranscriptSegment, TranscriptVersion
from app.services.embedding_service import DeterministicEmbedding
from app.services.index_service import index_service
from app.services.retrieval_service import retrieval_service
from app.services.scope import ScopeContext
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[1]  # backend/
OUT = ROOT.parent / "docs" / "classroom-agent" / "reset-v1" / "evidence" / "phase4"
OUT.mkdir(parents=True, exist_ok=True)

SUBJECT_A = "subject-a"
RESULTS: list[dict] = []
FIX: dict = {}

# 唯一标记词（互不重叠，用于可靠断言召回/排除边界）
MARK_DOC = "DOCSTACK_PAGE1"          # L1 专属课件第 1 页
MARK_EXAMPLE = "EXAMPLECALC_3PLUS2"  # L1 已发布笔记中的结构化例题
MARK_TRANS = "LINMARK_ALPHA"         # L1 转写标记
MARK_SIBLING = "L2SIBLING_BETA"      # 同课程 L2 转写标记（兄弟课时）
MARK_OTHER = "C2OTHER_GAMMA"         # 其他课程 C2 转写标记
# 资料外标记：用与所有材料字符集完全不相交的罕见字，确保向量/词法两路都无任何共享 n-gram，
# 使「无匹配 → no_evidence → 依据不足」路径在确定性 embedding 下也可达。
MARK_NONE = "龘靐齉"


def record(pid: str, check: str, ok: bool, detail) -> None:
    row = dict(id=pid, check=check, result="通过" if ok else "不通过", detail=detail)
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


SOURCE_FILES = [
    "app/services/chat_service.py",
    "app/services/retrieval_service.py",
    "app/api/chat.py",
    "scripts/verify_resetv1_phase4.py",
    "../docs/classroom-agent/reset-v1/03-acceptance-v1.1.md",
    "../frontend/src/features/retrieval/pages/classroom-qa-page.tsx",
    "../frontend/src/api/chat.ts",
    "../frontend/src/router/routes.tsx",
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


async def add_lecture(course_id: uuid.UUID, title: str) -> uuid.UUID:
    """在同课程下新增课时（用于构造兄弟课时）。"""
    async with async_session_factory() as db:
        meeting = Meeting(course_id=course_id, title=title, status="pending")
        db.add(meeting)
        await db.flush()
        mid = meeting.id
        await db.commit()
        return mid


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


async def add_document(
    course_id: uuid.UUID, lecture_id: uuid.UUID | None, title: str, blocks: list[dict]
) -> tuple[uuid.UUID, uuid.UUID]:
    """直接构造已解析课件：SourceDocument + SourceDocumentVersion + ParsedBlock。"""
    async with async_session_factory() as db:
        doc = SourceDocument(
            course_id=course_id, lecture_id=lecture_id, title=title,
            filename=f"{title}.pdf", storage_key=f"docs/{title}.pdf",
            mime="application/pdf", content_hash=f"hash-{title}",
        )
        db.add(doc)
        await db.flush()
        ver = SourceDocumentVersion(
            source_document_id=doc.id, version=1, status="active",
            parse_status="parsed", block_count=len(blocks),
        )
        db.add(ver)
        await db.flush()
        for i, b in enumerate(blocks):
            db.add(ParsedBlock(
                document_version_id=ver.id, block_id=b["block_id"], seq=i,
                kind=b.get("kind", "text"), text=b["text"],
                heading=b.get("heading"), page_no=b.get("page_no"),
                paragraph_index=b.get("paragraph_index"),
                table_index=b.get("table_index"),
            ))
        await db.commit()
        return doc.id, ver.id


async def add_note(lecture_id: uuid.UUID, content: dict) -> uuid.UUID:
    """直接构造已发布笔记（结构化例题，confirmed 才进入正式知识来源）。"""
    async with async_session_factory() as db:
        meeting = await db.get(Meeting, lecture_id)
        note = NoteVersion(
            lecture_id=lecture_id, status="published", content=content,
            published_at=datetime.now(timezone.utc),
        )
        db.add(note)
        await db.flush()
        meeting.active_note_version_id = note.id
        await db.commit()
        return note.id


async def build_course_index(course_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        idx = await index_service.build_index(db, course_id, embed=DeterministicEmbedding(1024))
        await index_service.activate(db, idx)


# ── 课堂问答 SSE 入口（端到端） ──────────────────────────────────────

async def http_classroom_chat(client, query, course_id=None, lecture_id=None) -> dict:
    body = {"query": query}
    if course_id is not None:
        body["course_id"] = str(course_id)
    if lecture_id is not None:
        body["lecture_id"] = str(lecture_id)
    r = await client.post("/api/chat/classroom/stream", json=body)
    assert r.status_code == 200, f"classroom chat HTTP {r.status_code}: {r.text}"
    events = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            data = line[6:].strip()
            if data:
                events.append(json.loads(data))
    types = [e.get("type") for e in events]
    sources = next((e for e in events if e.get("type") == "sources"), {})
    tokens = [e for e in events if e.get("type") == "token"]
    done = next((e for e in events if e.get("type") == "done"), None)
    answer = "".join(e.get("content", "") for e in tokens)
    return {
        "status": sources.get("status"),
        "results": sources.get("results") or [],
        "trace": sources.get("trace") or {},
        "answer": answer,
        "types": types,
        "done": done,
    }


def _result_contains(results: list[dict], marker: str) -> bool:
    return any(
        marker in c for r in results for c in (r.get("content") or [])
    )


def _sse_streamed(resp: dict) -> bool:
    """流式响应：sources 在前，随后 token，最后 done。"""
    t = resp["types"]
    return (
        t and t[0] == "sources"
        and "token" in t
        and t[-1] == "done"
        and t.index("sources") < t.index("token") < t.index("done")
    )


def _two_path(resp: dict) -> bool:
    tr = resp["trace"]
    return (
        "vector" in tr.get("branches", [])
        and "lexical" in tr.get("branches", [])
        and (tr.get("fused") or 0) >= 1
    )


# ── A5 / R5 场景 ─────────────────────────────────────────────────────

async def a5_courseware_detail(current, client):
    c1 = FIX["c1"]
    q = MARK_DOC
    resp = await http_classroom_chat(client, q, course_id=c1)
    top = resp["results"][0] if resp["results"] else {}
    top_content = "".join(top.get("content") or [])
    ok = (
        resp["status"] == "ok"
        and _sse_streamed(resp)
        and _two_path(resp)
        and top.get("source_type") == "document"
        and top.get("page_no") == 1
        and MARK_DOC in top_content
        and "课件" in resp["answer"]
        and MARK_DOC in resp["answer"]
    )
    record(
        "A5a", "课件细节：范围检索命中专属课件第 1 页，流式 sources/token/done + 两路融合记录",
        ok,
        dict(query=q, status=resp["status"], sse_types=resp["types"],
             trace=resp["trace"],
             top=dict(source_type=top.get("source_type"), page_no=top.get("page_no"),
                      block_id=top.get("block_id"), content=top_content[:80]),
             answer=resp["answer"][:200],
             two_path=_two_path(resp), streamed=_sse_streamed(resp)),
    )


async def a5_structured_example(current, client):
    c1 = FIX["c1"]
    q = MARK_EXAMPLE
    resp = await http_classroom_chat(client, q, course_id=c1)
    top = resp["results"][0] if resp["results"] else {}
    top_content = "".join(top.get("content") or [])
    ok = (
        resp["status"] == "ok"
        and top.get("source_type") == "note"
        and MARK_EXAMPLE in top_content
        and "笔记" in resp["answer"]
    )
    record(
        "A5b", "结构化例题：命中已发布笔记中的例题（note 来源），引用对应原文",
        ok,
        dict(query=q, status=resp["status"],
             top=dict(source_type=top.get("source_type"),
                      source_lecture_id=top.get("source_lecture_id"),
                      content=top_content[:120]),
             answer=resp["answer"][:200]),
    )


async def a5_transcript_recall(current, client):
    c1 = FIX["c1"]
    l1 = FIX["l1"]
    q = MARK_TRANS
    resp = await http_classroom_chat(client, q, course_id=c1, lecture_id=l1)
    top = resp["results"][0] if resp["results"] else {}
    top_content = "".join(top.get("content") or [])
    ok = (
        resp["status"] == "ok"
        and top.get("source_type") == "transcript"
        and top.get("start_ms") is not None
        and MARK_TRANS in top_content
    )
    record(
        "A5c", "资料路转写召回：本课时提问命中转写段并带音频定位（start_ms）",
        ok,
        dict(query=q, status=resp["status"],
             top=dict(source_type=top.get("source_type"),
                      start_ms=top.get("start_ms"), end_ms=top.get("end_ms"),
                      source_audio_asset_id=top.get("source_audio_asset_id"),
                      content=top_content[:80])),
    )


async def a5_out_of_material(current, client):
    c1 = FIX["c1"]
    q = MARK_NONE
    resp = await http_classroom_chat(client, q, course_id=c1)
    ok = (
        resp["status"] == "no_evidence"
        and "依据不足" in resp["answer"]
        and _sse_streamed(resp)
    )
    record(
        "A5d", "资料外问题：no_evidence 且流式回答明确提示依据不足",
        ok,
        dict(query=q, status=resp["status"], answer=resp["answer"],
             sse_types=resp["types"], total=len(resp["results"])),
    )


async def a5_lecture_scope_excludes_sibling(current, client):
    c1 = FIX["c1"]
    l1 = FIX["l1"]
    q = MARK_SIBLING
    resp = await http_classroom_chat(client, q, course_id=c1, lecture_id=l1)
    # 课时范围须排除兄弟课时 L2 的转写，任何返回正文都不得含其标记词。
    ok = not _result_contains(resp["results"], MARK_SIBLING)
    record(
        "A5e", "课时范围排除兄弟课时：L1 范围检索不到 L2 专属标记词",
        ok,
        dict(query=q, status=resp["status"], total=len(resp["results"]),
             results=[dict(source_type=r.get("source_type"), content="".join(r.get("content") or [])[:60])
                      for r in resp["results"]]),
    )


async def a5_course_scope_includes_other_lecture(current, client):
    c1 = FIX["c1"]
    q = MARK_SIBLING
    resp = await http_classroom_chat(client, q, course_id=c1)
    ok = resp["status"] == "ok" and _result_contains(resp["results"], MARK_SIBLING)
    record(
        "A5f", "课程范围可查该课其他课时：C1 范围检索到 L2 标记词",
        ok,
        dict(query=q, status=resp["status"], total=len(resp["results"]),
             hits=[dict(source_type=r.get("source_type"), source_lecture_id=r.get("source_lecture_id"))
                   for r in resp["results"] if _result_contains([r], MARK_SIBLING)]),
    )


async def a5_course_scope_excludes_other_course(current, client):
    c1 = FIX["c1"]
    c2 = FIX["c2"]
    q = MARK_OTHER
    resp_c1 = await http_classroom_chat(client, q, course_id=c1)
    resp_c2 = await http_classroom_chat(client, q, course_id=c2)
    exclude_ok = not _result_contains(resp_c1["results"], MARK_OTHER)
    include_ok = resp_c2["status"] == "ok" and _result_contains(resp_c2["results"], MARK_OTHER)
    ok = exclude_ok and include_ok
    record(
        "A5g", "当前课程范围排除其他课程标记词和材料（含正向对照：C2 可查自身）",
        ok,
        dict(query=q,
             c1=dict(status=resp_c1["status"], contains=_result_contains(resp_c1["results"], MARK_OTHER)),
             c2=dict(status=resp_c2["status"], contains=_result_contains(resp_c2["results"], MARK_OTHER)),
             c1_total=len(resp_c1["results"]), c2_total=len(resp_c2["results"])),
    )


def tests(current, client):
    return [
        ("A5a", a5_courseware_detail(current, client)),
        ("A5b", a5_structured_example(current, client)),
        ("A5c", a5_transcript_recall(current, client)),
        ("A5d", a5_out_of_material(current, client)),
        ("A5e", a5_lecture_scope_excludes_sibling(current, client)),
        ("A5f", a5_course_scope_includes_other_lecture(current, client)),
        ("A5g", a5_course_scope_excludes_other_course(current, client)),
    ]


async def main() -> None:
    started = datetime.now(timezone.utc)
    await create_db(MAIN_DB)
    run_alembic("head", asyncpg_dsn(MAIN_DB))

    # ── 构造夹具：C1(L1,L2) + C2(L3)，含转写 / 课件 / 已发布笔记 ──
    l1, c1 = await create_lecture(SUBJECT_A, "p4-c1-l1")
    l2 = await add_lecture(c1, "p4-c1-l2")
    l3, c2 = await create_lecture(SUBJECT_A, "p4-c2-l3")
    FIX.update(c1=c1, c2=c2, l1=l1, l2=l2, l3=l3)

    await add_transcript(l1, [_seg(0, f"线性表定义讲解 {MARK_TRANS} 顺序存储结构", start_ms=0, end_ms=1000)])
    await add_transcript(l2, [_seg(0, f"二叉树遍历 {MARK_SIBLING} 先序中序后序", start_ms=0, end_ms=1000)])
    await add_transcript(l3, [_seg(0, f"图论最短路径 {MARK_OTHER} Dijkstra", start_ms=0, end_ms=1000)])

    await add_document(c1, l1, "p4-课件", [
        {"block_id": "p1", "page_no": 1, "text": f"栈的抽象数据类型 {MARK_DOC} 入栈与出栈操作", "kind": "text"},
        {"block_id": "p2", "page_no": 2, "text": "队列先进先出 FIFO", "kind": "text"},
    ])

    await add_note(l1, {
        "title": "线性表课堂笔记",
        "outline": ["要点"],
        "sections": [{"heading": "要点", "points": []}],
        "key_conclusions": [],
        "knowledge_points": [],
        "examples": [
            {"status": "confirmed",
             "problem": f"例题：{MARK_EXAMPLE} 计算 2+3",
             "derivation": "2+3=5",
             "evidence_ids": []},
        ],
        "key_points": [],
    })

    await build_course_index(c1)
    await build_course_index(c2)

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
            "index_embedding_provider": settings.INDEX_EMBEDDING_PROVIDER,
            "index_embedding_dimensions": settings.INDEX_EMBEDDING_DIMENSIONS,
            "index_top_k": settings.INDEX_TOP_K,
            "index_rrf_k": settings.INDEX_RRF_K,
            "index_candidate_pool": settings.INDEX_CANDIDATE_POOL,
            "index_context_budget": settings.INDEX_CONTEXT_BUDGET,
            "index_relevance_threshold": settings.INDEX_RETRIEVAL_RELEVANCE_THRESHOLD,
            "classroom_paid_calls_enabled": settings.CLASSROOM_PAID_CALLS_ENABLED,
            "database": MAIN_DB,
            "embedding": "DeterministicEmbedding（无真实供应商调用）",
            "markers": dict(doc=MARK_DOC, example=MARK_EXAMPLE, transcript=MARK_TRANS,
                            sibling=MARK_SIBLING, other=MARK_OTHER, none=MARK_NONE),
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
