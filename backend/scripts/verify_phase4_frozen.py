r"""Phase 04「检索闭环」冻结验收：P04-01～P04-16（P0）独立验证。

针对 docs/classroom-agent/acceptance/phase-04-frozen-checklist-v1.md。
P1（P04-17/18/19）转人工，不在本脚本自动验证。

运行（backend 目录）：
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  .\.venv\Scripts\python.exe scripts/verify_phase4_frozen.py
"""

import asyncio
import io
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.update(
    OPENAI_API_KEY="", DASHSCOPE_API_KEY="", PYTHONPATH=".", PYTHONUTF8="1",
    INDEX_EMBEDDING_PROVIDER="deterministic", CLASSROOM_PAID_CALLS_ENABLED="false",
)

import asyncpg
from app.config import settings

BASE = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
PREFIX = "yuan_phase4_test_" + uuid.uuid4().hex[:10] + "_"
MAIN_DB = PREFIX + "main"

# 在导入会创建引擎的模块前，把引擎指向隔离测试库，绝不触碰生产库 yuan_meet。
settings.DATABASE_URL = BASE.replace("postgresql://", "postgresql+asyncpg://") + "/" + MAIN_DB
os.environ["DATABASE_URL"] = settings.DATABASE_URL

from app.api.deps import get_scope
from app.db.session import async_session_factory
from app.main import app
from app.models.audio_asset import AudioAsset
from app.models.course import Course
from app.models.evidence import EvidenceSpan
from app.models.index_version import IndexBlock, IndexVersion
from app.models.knowledge_doc import KnowledgeDocument
from app.models.meeting import Meeting
from app.models.note_version import NoteVersion
from app.models.organize import IndexIntent
from app.models.parsed_block import ParsedBlock
from app.models.processing_job import ProcessingJob
from app.models.source_document import SourceDocument, SourceDocumentVersion
from app.models.transcript_version import TranscriptSegment, TranscriptVersion
import app.services.index_service as index_module
import app.services.retrieval_service as retrieval_module
from app.services.document_parser import document_parser
from app.services.document_service import document_service
from app.services.embedding_service import DeterministicEmbedding, EmbeddingProvider
from app.services.evidence_service import evidence_service
from app.services.index_service import index_service
from app.services.retrieval_service import retrieval_service
from app.services.scope import ScopeContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

ROOT = Path(__file__).resolve().parents[1]  # backend/
OUT = ROOT.parent / "docs" / "classroom-agent" / "acceptance" / "evidence-phase4-20261029"
OUT.mkdir(parents=True, exist_ok=True)

# 隔离上传目录，避免写入生产 ./uploads
_UPLOAD_TMP = OUT / "_uploads"
_UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
settings.UPLOAD_DIR = str(_UPLOAD_TMP)

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
    "app/services/retrieval_service.py",
    "app/services/index_service.py",
    "app/services/document_service.py",
    "app/services/document_parser.py",
    "app/services/document_chunker.py",
    "app/services/evidence_service.py",
    "app/services/embedding_service.py",
    "app/config.py",
    "app/models/index_version.py",
    "app/models/source_document.py",
    "app/models/parsed_block.py",
    "app/api/documents.py",
    "app/api/retrieval.py",
    "app/api/lectures.py",
    "app/api/courses.py",
    "app/schemas/document.py",
    "app/schemas/retrieval.py",
    "alembic/versions/2026_10_29_0002-c4d5e6f7a8b9_add_phase4_retrieval.py",
    "scripts/verify_phase4_frozen.py",
    "../docs/classroom-agent/acceptance/phase-04-frozen-checklist-v1.md",
    "../frontend/src/features/retrieval/pages/retrieval-page.tsx",
    "../frontend/src/features/retrieval/api.ts",
    "../frontend/src/router/routes.tsx",
    "../frontend/src/components/layout/sidebar.tsx",
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


async def expire_lease(job_id: uuid.UUID) -> None:
    """模拟旧 worker 失联：租约过期，允许新执行者接管（CA-11 claim 语义）。"""
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, job_id)
        j.lease_expires_at = now() - timedelta(seconds=10)
        await db.commit()


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


# ── 固定样本生成（真实格式，不是手写 ParsedBlock） ──────────────────────


def _esc_pdf(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(pages: list[str]) -> bytes:
    """手工构造文本型 PDF（Helvetica/Type1，latin-1 正文），多页保留页码。"""
    n = len(pages)
    font_obj = 3 + 2 * n
    objs = {}
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = (
        "<< /Type /Pages /Kids [%s] /Count %d >>"
        % (" ".join("%d 0 R" % i for i in range(3, 3 + n)), n)
    ).encode()
    for idx in range(n):
        pn = 3 + idx
        cn = 3 + n + idx
        objs[pn] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            "/Resources << /Font << /F1 %d 0 R >> >> >>" % (cn, font_obj)
        ).encode()
    for idx in range(n):
        cn = 3 + n + idx
        stream = "BT /F1 12 Tf 72 720 Td (%s) Tj ET" % _esc_pdf(pages[idx])
        b = stream.encode("latin-1")
        objs[cn] = ("<< /Length %d >>\nstream\n" % len(b)).encode() + b + b"\nendstream"
    objs[font_obj] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    return _assemble_pdf(objs)


def make_image_pdf() -> bytes:
    """图片型 PDF：仅图形、无可提取文本 → 解析器需报 needs_ocr。"""
    stream = "q 0.5 0.5 0.5 RG 72 72 100 100 re f Q"
    b = stream.encode("latin-1")
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << >> >>",
        4: ("<< /Length %d >>\nstream\n" % len(b)).encode() + b + b"\nendstream",
    }
    return _assemble_pdf(objs)


def _assemble_pdf(objs: dict) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offs = {}
    total = max(objs)
    for num in range(1, total + 1):
        offs[num] = len(out)
        out += ("%d 0 obj\n" % num).encode() + objs[num] + b"\nendobj\n"
    xref = len(out)
    out += ("xref\n0 %d\n" % (total + 1)).encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, total + 1):
        out += ("%010d 00000 n \n" % offs[num]).encode()
    out += (
        "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (total + 1, xref)
    ).encode()
    return bytes(out)


def make_docx() -> bytes:
    from docx import Document
    d = Document()
    d.add_heading("Chapter One", level=1)
    d.add_paragraph("para anchor PARA-A")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "c1"
    t.rows[0].cells[1].text = "TABLE-ANCHOR"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_doc() -> bytes:
    # OLE2 复合文档魔数（旧 DOC），解析器按扩展名报 unsupported_conversion
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32


def make_pptx() -> bytes:
    # PPTX 按扩展名报 unsupported（本轮不支持）
    return b"PK\x03\x04" + b"\x00" * 64


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


async def set_archived(course_id: uuid.UUID, archived: bool) -> None:
    async with async_session_factory() as db:
        c = await db.get(Course, course_id)
        c.archived_at = now() if archived else None
        await db.commit()


def _seg(seq, content, start_ms=None, end_ms=None) -> dict:
    return dict(seq_index=seq, speaker="t", content=content,
                start_ms=start_ms, end_ms=end_ms)


async def add_transcript(
    lecture_id: uuid.UUID, segments: list[dict], *, is_demo: bool = False, active: bool = True
) -> tuple[uuid.UUID, uuid.UUID]:
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


async def make_transcript_evidence(
    scope: ScopeContext, transcript_version_id, seg_start, seg_end, quote
) -> uuid.UUID:
    async with async_session_factory() as db:
        span = await evidence_service.create_evidence(
            db, scope,
            transcript_version_id=transcript_version_id,
            segment_start=seg_start, segment_end=seg_end,
            quote=quote,
        )
        await db.commit()
        return span.id


async def make_document_evidence(
    scope: ScopeContext, document_version_id, block_id, quote
) -> uuid.UUID:
    async with async_session_factory() as db:
        span = await evidence_service.create_evidence(
            db, scope,
            document_version_id=document_version_id,
            block_id=block_id, quote=quote,
        )
        await db.commit()
        return span.id


async def add_published_note(
    lecture_id, transcript_version_id, title, point_text, evidence_ids=None
) -> uuid.UUID:
    """直接落一份已发布笔记（含真实证据关联），供索引与来源解析。"""
    async with async_session_factory() as db:
        note = NoteVersion(
            lecture_id=lecture_id,
            transcript_version_id=transcript_version_id,
            status="published",
            published_at=now(),
            content={
                "title": title,
                "sections": [{"heading": "要点", "points": [{"text": point_text}]}],
                "key_conclusions": [
                    {"text": point_text, "quote": point_text, "segment_index": 0,
                     "evidence_ids": evidence_ids or [],
                     "status": "confirmed", "origin": "teacher_explicit"}
                ],
            },
        )
        db.add(note)
        await db.flush()
        await db.refresh(note)
        nid = note.id
        await db.commit()
        return nid


async def add_index_intent(note_id, source_version_id, status="pending") -> uuid.UUID:
    async with async_session_factory() as db:
        intent = IndexIntent(
            note_version_id=note_id, source_version_id=source_version_id, status=status
        )
        db.add(intent)
        await db.flush()
        await db.refresh(intent)
        iid = intent.id
        await db.commit()
        return iid


class FailingEmbed(EmbeddingProvider):
    """确定性失败替身：embed_batch 返回 None，构建明确失败。"""

    model = "fake-failing"
    dimensions = 1024

    async def embed_batch(self, texts):
        return [None] * len(texts)


async def build_active_index(course_id: uuid.UUID) -> uuid.UUID:
    async with async_session_factory() as db:
        iv = await index_service.build_index(db, course_id)
        await index_service.activate(db, iv)
        return iv.id


async def upload(client, course_id, filename, content: bytes, lecture_id=None,
                 mime="application/octet-stream"):
    params = {"lecture_id": str(lecture_id)} if lecture_id is not None else None
    return await client.post(
        f"/api/courses/{course_id}/documents",
        files={"file": (filename, content, mime)},
        params=params,
    )


async def search(client, query, course_id=None, lecture_id=None, top_k=None):
    return await client.post("/api/retrieval/search", json={
        "query": query,
        "course_id": str(course_id) if course_id else None,
        "lecture_id": str(lecture_id) if lecture_id else None,
        "top_k": top_k,
    })


def doc_ids_from_results(body: dict) -> list[str]:
    return [r["source_document_id"] for r in body["results"] if r.get("source_document_id")]


def has_content_anchor(body: dict, anchor: str) -> bool:
    """任一结果的原文内容包含锚点（用于转写/笔记/文档来源，避免依赖 no_evidence）。"""
    return any(
        anchor in c for r in body["results"] for c in (r.get("content") or [])
    )


# ── 主流程 ────────────────────────────────────────────────────────────


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
                "embedding_model": settings.INDEX_EMBEDDING_MODEL,
                "embedding_dimensions": settings.INDEX_EMBEDDING_DIMENSIONS,
                "content_hash_algo": settings.INDEX_CONTENT_HASH_ALGO,
                "chunk_size": settings.INDEX_CHUNK_SIZE,
                "chunk_overlap": settings.INDEX_CHUNK_OVERLAP,
                "candidate_pool": settings.INDEX_CANDIDATE_POOL,
                "top_k": settings.INDEX_TOP_K,
                "rrf_k": settings.INDEX_RRF_K,
                "context_budget": settings.INDEX_CONTEXT_BUDGET,
                "single_source_limit": settings.INDEX_SINGLE_SOURCE_LIMIT,
                "relevance_threshold": settings.INDEX_RETRIEVAL_RELEVANCE_THRESHOLD,
                "lag_strategy": settings.INDEX_LAG_STRATEGY,
                "max_attempts": settings.INDEX_MAX_ATTEMPTS,
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
        ("P04-01", p04_01(current, client)),
        ("P04-02", p04_02(current, client)),
        ("P04-03", p04_03(current, client)),
        ("P04-04", p04_04(current, client)),
        ("P04-05", p04_05(current, client)),
        ("P04-06", p04_06(current, client)),
        ("P04-07", p04_07(current, client)),
        ("P04-08", p04_08(current, client)),
        ("P04-09", p04_09(current, client)),
        ("P04-10", p04_10(current, client)),
        ("P04-11", p04_11(current, client)),
        ("P04-12", p04_12(current, client)),
        ("P04-13", p04_13(current, client)),
        ("P04-14", p04_14(current, client)),
        ("P04-15", p04_15(current, client)),
        ("P04-16", p04_16()),
    ]


# ── P04-01 文档/索引/检索/证据贯穿主体、课程与课时范围 ──────────────────
async def p04_01(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0401-L1")
    l2, _ = await create_lecture(SUBJECT_A, "p0401-L2")
    async with async_session_factory() as db:
        m2 = await db.get(Meeting, l2)
        m2.course_id = c1  # L2 归入课程 C1，验证课时范围隔离
        await db.commit()
    l3, c2 = await create_lecture(SUBJECT_A, "p0401-L3")

    # C1：D0 公共课件 + L1 专属 D1 + L2 专属 D2；C2：L3 专属 D3
    r_d0 = await upload(client, c1, "d0.txt", b"PUBLIC-D0-ANCHOR", lecture_id=None, mime="text/plain")
    r_d1 = await upload(client, c1, "d1.txt", b"L1-ONLY-ANCHOR", lecture_id=l1, mime="text/plain")
    r_d2 = await upload(client, c1, "d2.txt", b"L2-ONLY-ANCHOR", lecture_id=l2, mime="text/plain")
    r_d3 = await upload(client, c2, "d3.txt", b"C2-ONLY-ANCHOR", lecture_id=l3, mime="text/plain")
    d0_id, d1_id, d2_id, d3_id = (r.json()["id"] for r in (r_d0, r_d1, r_d2, r_d3))
    assert all(r.status_code == 201 for r in (r_d0, r_d1, r_d2, r_d3))

    await build_active_index(c1)
    await build_active_index(c2)

    # 课程范围：整门 C1 含公共 + 各课时专属
    s_c1 = await search(client, "L1-ONLY-ANCHOR", course_id=c1)
    s_c1_l2 = await search(client, "L2-ONLY-ANCHOR", course_id=c1)
    # 课时范围 L1：仅 L1 专属 + C1 公共，不含 L2 专属
    s_l1_hit = await search(client, "L1-ONLY-ANCHOR", course_id=c1, lecture_id=l1)
    s_l1_d0 = await search(client, "PUBLIC-D0-ANCHOR", course_id=c1, lecture_id=l1)
    s_l1_miss_l2 = await search(client, "L2-ONLY-ANCHOR", course_id=c1, lecture_id=l1)
    # C1 不混 C2
    s_c1_miss_c2 = await search(client, "C2-ONLY-ANCHOR", course_id=c1)

    whole_course = (
        str(d1_id) in doc_ids_from_results(s_c1.json())
        and str(d2_id) in doc_ids_from_results(s_c1_l2.json())
    )
    l1_scope = (
        str(d1_id) in doc_ids_from_results(s_l1_hit.json())
        and str(d0_id) in doc_ids_from_results(s_l1_d0.json())
        and str(d2_id) not in doc_ids_from_results(s_l1_miss_l2.json())
    )
    no_cross_course = str(d3_id) not in doc_ids_from_results(s_c1_miss_c2.json())

    # 他主 B：对 C1 上传/列表/删除/检索均 404，且无副作用
    current["scope"] = ScopeContext(subject_id=SUBJECT_B)
    r_b_upload = await upload(client, c1, "x.txt", b"X", mime="text/plain")
    r_b_list = await client.get(f"/api/courses/{c1}/documents")
    r_b_del = await client.delete(f"/api/documents/{d1_id}")
    r_b_search = await search(client, "L1-ONLY-ANCHOR", course_id=c1)
    b_ok = (
        r_b_upload.status_code == 404 and r_b_list.status_code == 404
        and r_b_del.status_code == 404 and r_b_search.status_code == 404
    )
    current["scope"] = None

    # 冲突：把 L1 归属到 C2 上传 → 拒绝
    r_conflict = await upload(client, c2, "conflict.txt", b"Y", lecture_id=l1, mime="text/plain")
    conflict_ok = r_conflict.status_code == 404

    # 归档 C1 后立即停止暴露
    await set_archived(c1, True)
    r_arch_doc = await client.get(f"/api/courses/{c1}/documents")
    r_arch_search = await search(client, "L1-ONLY-ANCHOR", course_id=c1)
    arch_ok = r_arch_doc.status_code == 404 and r_arch_search.status_code == 404

    async with async_session_factory() as db:
        doc_count = (await db.execute(
            select(func.count(SourceDocument.id)).where(SourceDocument.course_id == c1)
        )).scalar_one()

    record(
        "P04-01",
        "上传/列表/删除/检索贯穿主体课程课时范围 + 冲突与归档拒绝无副作用",
        whole_course and l1_scope and no_cross_course and b_ok and conflict_ok
        and arch_ok and doc_count == 3,
        dict(whole_course=whole_course, l1_scope=l1_scope, no_cross_course=no_cross_course,
             b_upload=r_b_upload.status_code, b_list=r_b_list.status_code,
             b_del=r_b_del.status_code, b_search=r_b_search.status_code,
             conflict=r_conflict.status_code, arch_doc=r_arch_doc.status_code,
             arch_search=r_arch_search.status_code, c1_doc_count=doc_count),
    )


# ── P04-02 文件以稳定身份和版本管理，同名不合并 ────────────────────────
async def p04_02(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0402-L1")
    _, c2 = await create_lecture(SUBJECT_A, "p0402-L2")

    # 同课程同文件名不同正文 → 两份独立身份
    r1 = await upload(client, c1, "note.txt", b"content-A-ANCHOR", lecture_id=l1, mime="text/plain")
    r2 = await upload(client, c1, "note.txt", b"content-B-ANCHOR", lecture_id=l1, mime="text/plain")
    # 跨课程同文件名
    r3 = await upload(client, c2, "note.txt", b"content-C-ANCHOR", mime="text/plain")
    doc1, doc2, doc3 = r1.json()["id"], r2.json()["id"], r3.json()["id"]
    distinct = doc1 != doc2 != doc3 != doc1

    r_list = await client.get(f"/api/courses/{c1}/documents")
    list_ids = [d["id"] for d in r_list.json()]
    list_ok = r_list.status_code == 200 and doc1 in list_ids and doc2 in list_ids and doc3 not in list_ids
    filenames = [d["filename"] for d in r_list.json()]
    same_name_kept = filenames.count("note.txt") == 2

    # 对 doc1 建立后续解析版本（保留文件身份）
    async with async_session_factory() as db:
        v2 = await document_service.add_version(
            db, ScopeContext(subject_id=SUBJECT_A), uuid.UUID(doc1),
            filename="note.txt", content=b"content-A-v2-ANCHOR", mime="text/plain",
        )
        await db.commit()
    r_detail = await client.get(f"/api/documents/{doc1}")
    versions = r_detail.json()["versions"]
    v2_ok = v2.version == 2 and v2.source_document_id == uuid.UUID(doc1)
    detail_ok = (
        r_detail.status_code == 200 and len(versions) == 2
        and r_detail.json()["course_id"] == str(c1)
        and r_detail.json()["lecture_id"] == str(l1)
    )

    record(
        "P04-02",
        "同名不合并 + 跨课程独立 + 新版本仍关联文件身份",
        distinct and list_ok and same_name_kept and v2_ok and detail_ok,
        dict(distinct=distinct, list_ok=list_ok, same_name_kept=same_name_kept,
             v2_version=v2.version, v2_same_doc=v2.source_document_id == uuid.UUID(doc1),
             versions=versions),
    )


# ── P04-03 PDF/DOCX/TXT/Markdown 按原文块保留真实定位 ──────────────────
async def p04_03(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0403-L1")

    pdf = make_pdf(["Page one: heading A", "Page two: PDF-PAGE2-ANCHOR"])
    docx = make_docx()
    txt = "TXT 段落一 TXT-A-ANCHOR\n\nTXT 段落二 TXT-B-ANCHOR".encode("utf-8")
    md = "# 标题\n\n正文段落 MD-ANCHOR".encode("utf-8")

    r_pdf = await upload(client, c1, "a.pdf", pdf, lecture_id=l1, mime="application/pdf")
    r_docx = await upload(client, c1, "a.docx", docx, lecture_id=l1, mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    r_txt = await upload(client, c1, "a.txt", txt, lecture_id=l1, mime="text/plain")
    r_md = await upload(client, c1, "a.md", md, lecture_id=l1, mime="text/markdown")

    async def blocks(doc_id):
        r = await client.get(f"/api/documents/{doc_id}/blocks")
        return r.json()

    pdf_b = await blocks(r_pdf.json()["id"])
    pdf_ok = (
        len(pdf_b) == 2
        and any(b["block_id"] == "p1" and b["page_no"] == 1 for b in pdf_b)
        and any(b["page_no"] == 2 and "PDF-PAGE2-ANCHOR" in b["text"] for b in pdf_b)
    )

    docx_b = await blocks(r_docx.json()["id"])
    by_kind = {b["kind"]: b for b in docx_b}
    docx_ok = (
        "heading" in by_kind and by_kind["heading"]["heading"] == "Chapter One"
        and by_kind["heading"]["paragraph_index"] is not None
        and "paragraph" in by_kind and "PARA-A" in by_kind["paragraph"]["text"]
        and by_kind["paragraph"]["paragraph_index"] is not None
        and "table" in by_kind and by_kind["table"]["table_index"] == 0
        and "TABLE-ANCHOR" in by_kind["table"]["text"]
        and all(b["page_no"] is None for b in docx_b)  # DOCX 不伪造页码
    )

    txt_b = await blocks(r_txt.json()["id"])
    txt_ok = (
        len(txt_b) == 2
        and any(b["block_id"] == "txt-0" and b["paragraph_index"] == 0 and "TXT-A-ANCHOR" in b["text"] for b in txt_b)
        and any(b["paragraph_index"] == 1 and "TXT-B-ANCHOR" in b["text"] for b in txt_b)
    )

    md_b = await blocks(r_md.json()["id"])
    md_ok = (
        len(md_b) == 2
        and any(b["block_id"] == "txt-0" and b["paragraph_index"] == 0 for b in md_b)
        and any("MD-ANCHOR" in b["text"] for b in md_b)
    )

    record(
        "P04-03",
        "四类格式经真实解析器产出稳定 block_id + 真实定位",
        pdf_ok and docx_ok and txt_ok and md_ok,
        dict(pdf_ok=pdf_ok, docx_ok=docx_ok, txt_ok=txt_ok, md_ok=md_ok,
             pdf_blocks=pdf_b, docx_kinds=list(by_kind), txt_blocks=txt_b, md_blocks=md_b),
    )


# ── P04-04 空文本及不支持格式不伪装解析/索引成功 ────────────────────────
async def p04_04(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0404-L1")

    r_img = await upload(client, c1, "img.pdf", make_image_pdf(), lecture_id=l1, mime="application/pdf")
    r_doc = await upload(client, c1, "old.doc", make_doc(), lecture_id=l1, mime="application/msword")
    r_pptx = await upload(client, c1, "s.pptx", make_pptx(), lecture_id=l1, mime="application/vnd.openxmlformats-officedocument.presentationml.presentation")

    img = r_img.json(); doc = r_doc.json(); pptx = r_pptx.json()
    img_ok = img["parse_status"] == "needs_ocr" and img["status"] == "failed" and img["block_count"] == 0
    doc_ok = doc["parse_status"] == "unsupported_conversion" and doc["status"] == "failed"
    pptx_ok = pptx["parse_status"] == "unsupported" and pptx["status"] == "failed"

    # 索引构建不把失败文档当来源（无有效块）
    await build_active_index(c1)
    s = await search(client, "IMG-ANCHOR", course_id=c1)
    no_fake_index = s.json()["status"] in ("no_evidence",)

    async with async_session_factory() as db:
        blocks_total = (await db.execute(
            select(func.count(ParsedBlock.id)).where(
                ParsedBlock.document_version_id.in_(
                    select(SourceDocumentVersion.id).where(
                        SourceDocumentVersion.source_document_id.in_(
                            select(SourceDocument.id).where(SourceDocument.course_id == c1)
                        )
                    )
                )
            )
        )).scalar_one()

    record(
        "P04-04",
        "图片PDF/DOC/PPTX 明确失败状态，不伪装解析/索引成功",
        img_ok and doc_ok and pptx_ok and no_fake_index and blocks_total == 0,
        dict(img_parse=img["parse_status"], img_status=img["status"],
             doc_parse=doc["parse_status"], pptx_parse=pptx["parse_status"],
             blocks_total=blocks_total, search_status=s.json()["status"]),
    )


# ── P04-05 按文件身份删除，不误删同名文件且立即停止召回 ────────────────
async def p04_05(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0405-L1")
    _, c2 = await create_lecture(SUBJECT_A, "p0405-L2")

    # C1 同名 D3/D4；C2 同名 D5（正文均不同）
    r3 = await upload(client, c1, "same.txt", b"D3-ANCHOR", lecture_id=l1, mime="text/plain")
    r4 = await upload(client, c1, "same.txt", b"D4-ANCHOR", lecture_id=l1, mime="text/plain")
    r5 = await upload(client, c2, "same.txt", b"D5-ANCHOR", mime="text/plain")
    d3, d4, d5 = r3.json()["id"], r4.json()["id"], r5.json()["id"]

    await build_active_index(c1)
    await build_active_index(c2)
    s_d3 = await search(client, "D3-ANCHOR", course_id=c1)
    s_d4 = await search(client, "D4-ANCHOR", course_id=c1)

    # 建一条指向 D3 块的文档证据，用于删除后核对“历史证据不可读”
    async with async_session_factory() as db:
        d3v = (await db.execute(
            select(SourceDocumentVersion).where(SourceDocumentVersion.source_document_id == uuid.UUID(d3))
        )).scalars().first()
        d3b = (await db.execute(
            select(ParsedBlock).where(ParsedBlock.document_version_id == d3v.id)
        )).scalars().first()
    ev_id = await make_document_evidence(
        ScopeContext(subject_id=SUBJECT_A), d3v.id, d3b.block_id, d3b.text
    )
    r_ev_before = await client.get(f"/api/evidence/{ev_id}")

    # 按 D3 身份删除
    r_del = await client.delete(f"/api/documents/{d3}")

    r_list = await client.get(f"/api/courses/{c1}/documents")
    list_ids = [d["id"] for d in r_list.json()]
    d3_gone = d3 not in list_ids and d4 in list_ids

    r_c2_list = await client.get(f"/api/courses/{c2}/documents")
    d5_kept = any(d["id"] == d5 for d in r_c2_list.json())

    s_d3_after = await search(client, "D3-ANCHOR", course_id=c1)
    s_d4_after = await search(client, "D4-ANCHOR", course_id=c1)
    recall_ok = (
        str(d3) not in doc_ids_from_results(s_d3_after.json())
        and str(d4) in doc_ids_from_results(s_d4_after.json())
    )

    r_ev_after = await client.get(f"/api/evidence/{ev_id}")

    record(
        "P04-05",
        "按身份删除不扩散 + 其他课程同名保留 + 立即停止召回 + 历史证据不可读",
        s_d3.json()["total"] >= 1 and s_d4.json()["total"] >= 1 and r_del.status_code == 200
        and d3_gone and d5_kept and recall_ok
        and r_ev_before.status_code == 200 and r_ev_after.status_code == 404,
        dict(d3_before=s_d3.json()["total"], d4_before=s_d4.json()["total"],
             d3_gone=d3_gone, d5_kept=d5_kept,
             d3_after=s_d3_after.json()["status"], d4_after=s_d4_after.json()["total"],
             ev_before=r_ev_before.status_code, ev_after=r_ev_after.status_code),
    )


# ── P04-06 完整有效转写、课件及已发布笔记形成可追溯索引 ────────────────
async def p04_06(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0406-L1")

    # 长转写（首/中/尾锚点），覆盖到末尾
    segs = [_seg(i, f"第{i}段内容", start_ms=i * 1000, end_ms=(i + 1) * 1000 - 100) for i in range(6)]
    segs[0]["content"] = "开头事实 HEAD-ANCHOR"
    segs[3]["content"] = "中部事实 MID-ANCHOR"
    segs[5]["content"] = "末尾事实 TAIL-ANCHOR"
    _, v1 = await add_transcript(l1, segs)

    # 已发布笔记 N1（带真实证据），草稿 N2
    ev_id = await make_transcript_evidence(ScopeContext(subject_id=SUBJECT_A), v1, 5, 5, "末尾事实 TAIL-ANCHOR")
    n1 = await add_published_note(l1, v1, "N1标题", "笔记正文 NOTE-ANCHOR", evidence_ids=[str(ev_id)])
    async with async_session_factory() as db:
        n2 = NoteVersion(
            lecture_id=l1, transcript_version_id=v1, status="draft",
            content={"title": "N2草稿", "sections": [{"heading": "草稿", "points": [{"text": "草稿内容 DRAFT-ANCHOR"}]}]},
        )
        db.add(n2)
        await db.flush()
        n2_id = n2.id
        await db.commit()

    # 演示来源：同课程的 demo 转写（不作为正式召回）
    demo_l, _ = await create_lecture(SUBJECT_A, "p0406-demo")
    async with async_session_factory() as db:
        dm = await db.get(Meeting, demo_l)
        dm.course_id = c1
        await db.commit()
    await add_transcript(demo_l, [_seg(0, "演示内容 DEMO-ANCHOR", start_ms=0, end_ms=1000)], is_demo=True, active=True)

    # 上传课件
    await upload(client, c1, "doc.txt", "课件正文 DOC-ANCHOR".encode("utf-8"), lecture_id=l1, mime="text/plain")
    await build_active_index(c1)

    async with async_session_factory() as db:
        iv = (await db.execute(
            select(IndexVersion).where(IndexVersion.course_id == c1, IndexVersion.status == "active")
        )).scalars().first()
        blocks = (await db.execute(
            select(IndexBlock).where(IndexBlock.index_version_id == iv.id)
        )).scalars().all()
        trans = [b for b in blocks if b.source_type == "transcript"]
        notes = [b for b in blocks if b.source_type == "note"]
        docs = [b for b in blocks if b.source_type == "document"]

        tail_covered = any(b.segment_end == 5 and "TAIL-ANCHOR" in b.content for b in trans)
        note_ok = len(notes) == 1 and notes[0].metadata_ and notes[0].metadata_.get("note_evidence_ids") == [str(ev_id)] and notes[0].metadata_.get("note_status") == "published"
        doc_ok = len(docs) == 1 and "DOC-ANCHOR" in docs[0].content
        # 草稿 / 演示内容不得进入索引正文
        no_draft = all("DRAFT-ANCHOR" not in b.content for b in blocks)
        no_demo = all("DEMO-ANCHOR" not in b.content for b in blocks)

    record(
        "P04-06",
        "完整转写覆盖到末尾 + 课件/N1 保留来源定位 + 草稿与演示不作召回",
        tail_covered and note_ok and doc_ok and no_draft and no_demo,
        dict(tail_covered=tail_covered, note_count=len(notes),
             note_evidence=notes[0].metadata_ if notes else None,
             doc_ok=doc_ok, no_draft=no_draft, no_demo=no_demo),
    )


# ── P04-07 Embedding 身份、维度和内容摘要一致 ─────────────────────────
async def p04_07(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0407-L1")
    await add_transcript(l1, [_seg(0, "向量化正文 EMBED-ANCHOR", start_ms=0, end_ms=1000)])

    iv_id = await build_active_index(c1)
    async with async_session_factory() as db:
        iv = await db.get(IndexVersion, iv_id)
        cfg = iv.config or {}
        block = (await db.execute(
            select(IndexBlock).where(IndexBlock.index_version_id == iv_id)
        )).scalars().first()
        meta_ok = (
            cfg.get("embedding_model") == DeterministicEmbedding.model
            and cfg.get("embedding_dimensions") == settings.INDEX_EMBEDDING_DIMENSIONS
            and cfg.get("content_hash_algo") == settings.INDEX_CONTENT_HASH_ALGO
            and block.embedding is not None
            and len(block.embedding) == settings.INDEX_EMBEDDING_DIMENSIONS
        )

    # 注入维度不匹配 → 构建明确失败，不建新版本
    before_count = None
    async with async_session_factory() as db:
        before_count = (await db.execute(
            select(func.count(IndexVersion.id)).where(IndexVersion.course_id == c1)
        )).scalar_one()
    dim_failed = False
    try:
        async with async_session_factory() as db:
            await index_service.build_index(db, c1, embed=DeterministicEmbedding(dimensions=768))
    except Exception as e:  # noqa: BLE001
        dim_failed = (
            getattr(e, "code", "") == "invalid_embedding_dimension"
            or getattr(e, "status_code", None) == 422
        )
    async with async_session_factory() as db:
        after_count = (await db.execute(
            select(func.count(IndexVersion.id)).where(IndexVersion.course_id == c1)
        )).scalar_one()
        active_count = (await db.execute(
            select(func.count(IndexVersion.id)).where(
                IndexVersion.course_id == c1, IndexVersion.status == "active"
            )
        )).scalar_one()

    no_new_version = after_count == before_count and active_count == 1

    record(
        "P04-07",
        "向量与内容/模型/维度绑定 + 错误维度明确拒绝",
        meta_ok and dim_failed and no_new_version,
        dict(meta_ok=meta_ok, cfg=cfg, dim_failed=dim_failed,
             version_count=(before_count, after_count), active_count=active_count),
    )


# ── P04-08 两路真实召回后合并、重排，再截取最终 top_k ──────────────────
async def p04_08(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0408-L1")

    # 三个文档共享中文术语，其中 docB 含可控重排目标标记
    await upload(client, c1, "a.txt", "共享术语X 关于主题甲：条件A成立 DOCA-ANCHOR".encode("utf-8"), lecture_id=l1, mime="text/plain")
    await upload(client, c1, "b.txt", "共享术语X 关于主题乙：条件B成立 TARGET-RERANK".encode("utf-8"), lecture_id=l1, mime="text/plain")
    await upload(client, c1, "c.txt", "共享术语X 关于主题丙：条件C成立 DOCC-ANCHOR".encode("utf-8"), lecture_id=l1, mime="text/plain")
    await build_active_index(c1)

    # 默认路径：两路召回 + 融合 + 去重 + 规则重排
    r0 = await search(client, "共享术语X", course_id=c1, top_k=3)
    t0 = r0.json()["trace"]
    both_branches = "vector" in t0["branches"] and "lexical" in t0["branches"]
    default_ok = r0.json()["total"] == 3 and both_branches and t0["fused"] >= 3 and t0["deduped"] >= 3

    # 注入可控重排：把含 TARGET-RERANK 的候选提到首位，且重排后仍截取 top_k
    def _boost(query, fused):
        for it in fused:
            it["rerank_score"] = 100.0 if "TARGET-RERANK" in (it["block"].content or "") else 0.0
        fused.sort(key=lambda x: x["rerank_score"], reverse=True)
        return fused

    retrieval_module._rerank_fn = _boost
    try:
        r1 = await search(client, "共享术语X", course_id=c1, top_k=2)
    finally:
        retrieval_module._rerank_fn = None

    body = r1.json()
    t1 = body["trace"]
    doc_ids = doc_ids_from_results(body)
    rerank_first = body["results"][0]["source_document_id"] == str(
        (await _doc_id_by_anchor(c1, "TARGET-RERANK"))
    )
    truncate_after_rerank = (
        body["total"] == 2 and t1["fused"] >= 3 and t1["deduped"] >= 3 and t1["selected"] == 2
    )

    record(
        "P04-08",
        "两路候选融合去重重排后才截取 top_k + 可控重排改变最终顺序",
        default_ok and rerank_first and truncate_after_rerank,
        dict(default_ok=default_ok, trace_default=t0, trace_injected=t1,
             rerank_first=rerank_first, doc_ids=doc_ids),
    )


async def _doc_id_by_anchor(course_id, anchor) -> uuid.UUID:
    # 按课程收敛：ParsedBlock 全库同名锚点会命中前序测试残留的块，必须限定课程。
    async with async_session_factory() as db:
        doc_ids = select(SourceDocument.id).where(SourceDocument.course_id == course_id)
        ver_ids = select(SourceDocumentVersion.id).where(
            SourceDocumentVersion.source_document_id.in_(doc_ids)
        )
        block = (await db.execute(
            select(ParsedBlock).where(
                ParsedBlock.text.ilike(f"%{anchor}%"),
                ParsedBlock.document_version_id.in_(ver_ids),
            )
        )).scalars().first()
        ver = await db.get(SourceDocumentVersion, block.document_version_id)
        return ver.source_document_id


# ── P04-09 同源去重依赖来源定位，不误合并同词不同材料 ──────────────────
async def p04_09(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0409-L1")

    # 同一块被两路同时召回 → 去重为一条；两份不同来源含相同短词不同限定 → 均保留
    await upload(client, c1, "x.txt", "关于X：条件A成立 DEDUP-UNIQUE".encode("utf-8"), lecture_id=l1, mime="text/plain")
    await upload(client, c1, "y.txt", "关于X：条件B成立".encode("utf-8"), lecture_id=l1, mime="text/plain")
    await build_active_index(c1)

    # 精确命中 DEDUP-UNIQUE：同一来源块（向量+词法两路均命中）只出现一条
    r = await search(client, "DEDUP-UNIQUE", course_id=c1, top_k=5)
    body = r.json()
    t = body["trace"]
    uniq_ids = [x["source_document_id"] for x in body["results"]]
    no_dup_same_source = len(uniq_ids) == len(set(uniq_ids))
    hit_once = uniq_ids.count(str(await _doc_id_by_anchor(c1, "DEDUP-UNIQUE"))) == 1

    # 相同短词不同限定：两份不同来源都保留，不按字符相似度误合并
    r2 = await search(client, "关于X", course_id=c1, top_k=5)
    doc_ids = doc_ids_from_results(r2.json())
    both = (
        str(await _doc_id_by_anchor(c1, "条件A成立")) in doc_ids
        and str(await _doc_id_by_anchor(c1, "条件B成立")) in doc_ids
    )

    record(
        "P04-09",
        "同源同定位只一条 + 不同来源同短词不误删",
        no_dup_same_source and hit_once and both,
        dict(fused=t["fused"], deduped=t["deduped"], no_dup=no_dup_same_source,
             hit_once=hit_once, both=both, doc_ids=doc_ids),
    )


# ── P04-10 补取上下文受来源、版本和实际预算约束 ────────────────────────
async def p04_10(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0410-L1")
    l2, _ = await create_lecture(SUBJECT_A, "p0410-L2")
    async with async_session_factory() as db:
        m2 = await db.get(Meeting, l2)
        m2.course_id = c1  # 同课程另一课时：验证邻接不跨课时
        await db.commit()

    # 长段强制分块：命中段成为独立中间块，前后各有一块可作邻接
    pad = "字" * 600
    segs = [
        _seg(0, "前文 CONTEXT-BEFORE" + pad, start_ms=0, end_ms=1000),
        _seg(1, "命中 HIT-ANCHOR" + pad, start_ms=2000, end_ms=3000),
        _seg(2, "后文 CONTEXT-AFTER" + pad, start_ms=4000, end_ms=5000),
    ]
    _, v1 = await add_transcript(l1, segs)
    await add_transcript(l2, [_seg(0, "另一课时 INTERFERENCE" + pad, start_ms=0, end_ms=1000)])
    await build_active_index(c1)

    r = await search(client, "HIT-ANCHOR", course_id=c1, lecture_id=l1, top_k=5)
    res = [x for x in r.json()["results"] if x["source_type"] == "transcript"]
    assert res, r.json()
    hit = res[0]
    adjacent = hit["adjacent"]
    # 邻接来自同一版本：前文 + 后文均出现，不跨到另一课时
    adj_same_version = any("CONTEXT-BEFORE" in a for a in adjacent) and any(
        "CONTEXT-AFTER" in a for a in adjacent
    )
    no_cross = not any("INTERFERENCE" in a for a in adjacent)
    total_len = sum(len(x) for x in hit["content"]) + sum(len(x) for x in adjacent)
    within_budget = total_len <= settings.INDEX_CONTEXT_BUDGET
    within_limit = len(adjacent) <= settings.INDEX_SINGLE_SOURCE_LIMIT

    # 整次响应累计（P04-10 收口复核）：所有返回项的命中正文 + 邻接正文合计受绑定预算约束，
    # 不能只核算第一条命中（否则多命中会成倍超额）。
    response_chars = sum(
        sum(len(c) for c in x["content"]) + sum(len(a) for a in x["adjacent"])
        for x in res
    )
    total_within_budget = response_chars <= settings.INDEX_CONTEXT_BUDGET

    # 文档来源邻接上下文
    await upload(client, c1, "d.txt", "块一 BLK-ONE\n\n块二 BLK-TWO-HIT\n\n块三 BLK-THREE".encode("utf-8"), lecture_id=l1, mime="text/plain")
    await build_active_index(c1)
    r2 = await search(client, "BLK-TWO-HIT", course_id=c1, lecture_id=l1, top_k=5)
    doc_hit = next((x for x in r2.json()["results"] if x.get("block_id") == "txt-1"), None)
    doc_adj_ok = (
        doc_hit is not None
        and any(("BLK-ONE" in a or "BLK-THREE" in a) for a in doc_hit["adjacent"])
    )

    record(
        "P04-10",
        "邻接上下文同源同版本 + 整次响应预算/限额约束 + 不跨课时",
        adj_same_version and no_cross and within_budget and total_within_budget
        and within_limit and doc_adj_ok,
        dict(adjacent=adjacent, adj_same_version=adj_same_version, no_cross=no_cross,
             total_len=total_len, within_budget=within_budget,
             response_chars=response_chars, total_within_budget=total_within_budget,
             within_limit=within_limit, doc_adj_ok=doc_adj_ok),
    )


# ── P04-11 检索结果的统一证据能解析实际正文与定位 ──────────────────────
async def p04_11(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0411-L1")

    # 转写来源
    _, v1 = await add_transcript(l1, [_seg(0, "转写正文 TRANSCRIPT-EVID", start_ms=3000, end_ms=5000)])
    # 文档来源（PDF 第二页 + DOCX 表格/段落）
    await upload(client, c1, "p.pdf", make_pdf(["Page one", "Page two: PDF-P11-ANCHOR"]), lecture_id=l1, mime="application/pdf")
    await upload(client, c1, "d.docx", make_docx(), lecture_id=l1, mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    # 笔记来源
    ev_id = await make_transcript_evidence(ScopeContext(subject_id=SUBJECT_A), v1, 0, 0, "转写正文 TRANSCRIPT-EVID")
    n1 = await add_published_note(l1, v1, "N1", "笔记正文 NOTE-EVID", evidence_ids=[str(ev_id)])
    await build_active_index(c1)

    # 转写结果解析
    rt = await search(client, "TRANSCRIPT-EVID", course_id=c1, lecture_id=l1, top_k=5)
    t_hit = next((x for x in rt.json()["results"] if x["source_type"] == "transcript"), None)
    t_ok = (
        t_hit is not None and t_hit["start_ms"] == 3000 and t_hit["end_ms"] == 5000
        and t_hit["source_audio_asset_id"] is not None and t_hit["content"] == ["转写正文 TRANSCRIPT-EVID"]
    )

    # PDF 第二页
    rp = await search(client, "PDF-P11-ANCHOR", course_id=c1, lecture_id=l1, top_k=5)
    p_hit = next((x for x in rp.json()["results"] if x.get("page_no") == 2), None)
    p_ok = p_hit is not None and "PDF-P11-ANCHOR" in p_hit["content"][0]

    # DOCX 表格
    rd = await search(client, "TABLE-ANCHOR", course_id=c1, lecture_id=l1, top_k=5)
    d_hit = next((x for x in rd.json()["results"] if x.get("block_id") == "table-0"), None)
    d_ok = d_hit is not None and d_hit["table_index"] == 0

    # 笔记结果
    rn = await search(client, "NOTE-EVID", course_id=c1, lecture_id=l1, top_k=5)
    n_hit = next((x for x in rn.json()["results"] if x["source_type"] == "note"), None)
    n_ok = n_hit is not None and n_hit["note_evidence_ids"] == [str(ev_id)]

    # 非法证据：虚构 block / 越范围 block / 不匹配 quote
    async with async_session_factory() as db:
        docv = (await db.execute(
            select(SourceDocumentVersion).join(SourceDocument, SourceDocumentVersion.source_document_id == SourceDocument.id).where(SourceDocument.course_id == c1)
        )).scalars().first()
        real_block = (await db.execute(
            select(ParsedBlock).where(ParsedBlock.document_version_id == docv.id)
        )).scalars().first()
    scope_a = ScopeContext(subject_id=SUBJECT_A)
    async def try_doc_evidence(dvid, bid, quote):
        try:
            async with async_session_factory() as db:
                await evidence_service.create_evidence(
                    db, scope_a, document_version_id=dvid, block_id=bid, quote=quote
                )
                await db.commit()
            return 200
        except Exception as e:  # noqa: BLE001
            return getattr(e, "status_code", 500)

    fake_block = await try_doc_evidence(docv.id, "no-such-block", "x")
    bad_quote = await try_doc_evidence(docv.id, real_block.block_id, "不存在的引文")
    ok_quote = await try_doc_evidence(docv.id, real_block.block_id, real_block.text)

    # 合法旧证据直接读取
    old_ev = await make_document_evidence(scope_a, docv.id, real_block.block_id, real_block.text)
    r_old = await client.get(f"/api/evidence/{old_ev}")
    old_ok = (
        r_old.status_code == 200 and r_old.json()["source_type"] == "document"
        and r_old.json()["content"] == [real_block.text]
        and r_old.json()["block_id"] == real_block.block_id
    )

    record(
        "P04-11",
        "统一证据解析正文/定位 + 文档 quote 匹配真实块 + 非法拒绝 + 旧证据可读",
        t_ok and p_ok and d_ok and n_ok
        and fake_block == 404 and bad_quote == 422 and ok_quote == 200 and old_ok,
        dict(t_ok=t_ok, p_ok=p_ok, d_ok=d_ok, n_ok=n_ok,
             fake_block=fake_block, bad_quote=bad_quote, ok_quote=ok_quote, old_ok=old_ok),
    )


# ── P04-12 索引构建/切换原子，失败及旧任务不破坏当前版本 ───────────────
async def p04_12(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0412-L1")

    _, v1 = await add_transcript(l1, [_seg(0, "P12V1-ANCHOR", start_ms=0, end_ms=1000)])
    i1 = await build_active_index(c1)

    # (a) 同源重建失败 → I1 保留
    try:
        async with async_session_factory() as db:
            await index_service.build_index(db, c1, embed=FailingEmbed())
        a_failed = False
    except Exception:  # noqa: BLE001
        a_failed = True
    async with async_session_factory() as db:
        active_after_a = (await db.execute(
            select(IndexVersion).where(IndexVersion.course_id == c1, IndexVersion.status == "active")
        )).scalars().first()
    a_ok = a_failed and active_after_a is not None and active_after_a.id == i1

    # (b) V2 更新而 I2 仍 building：旧索引继续服务；恢复后切 I2
    _, v2 = await add_transcript(l1, [_seg(0, "P12V2-ANCHOR", start_ms=0, end_ms=1000)])
    async with async_session_factory() as db:
        job = await index_service.create_index_job(db, ScopeContext(subject_id=SUBJECT_A), c1)
        job_id = job.id

    def _boom():
        raise index_module.BuildInterrupted()

    index_module._build_hook = _boom
    crashed = False
    try:
        async with async_session_factory() as db:
            await index_service.run_index_job(db, job_id)
    except index_module.BuildInterrupted:
        crashed = True
    finally:
        index_module._build_hook = None

    # building 期间旧索引 I1 服务（V1 命中，V2 未命中），状态 building
    s_old = await search(client, "P12V1-ANCHOR", course_id=c1)
    s_new_pending = await search(client, "P12V2-ANCHOR", course_id=c1)
    r_status = await client.get(f"/api/lectures/{l1}/index-status")
    b_ok = (
        crashed and has_content_anchor(s_old.json(), "P12V1-ANCHOR")
        and not has_content_anchor(s_new_pending.json(), "P12V2-ANCHOR")
        and r_status.json()["status"] == "building"
    )

    # 恢复 → 激活 I2（旧 worker 失联：先显式过期租约，模拟失联后租约到期被接管）
    await expire_lease(job_id)
    async with async_session_factory() as db:
        await index_service.run_index_job(db, job_id)
    s_v1_after = await search(client, "P12V1-ANCHOR", course_id=c1)
    s_v2_after = await search(client, "P12V2-ANCHOR", course_id=c1)
    b_switch = (
        has_content_anchor(s_v2_after.json(), "P12V2-ANCHOR")
        and not has_content_anchor(s_v1_after.json(), "P12V1-ANCHOR")
    )

    async with async_session_factory() as db:
        i2 = (await db.execute(
            select(IndexVersion).where(IndexVersion.course_id == c1, IndexVersion.status == "active")
        )).scalars().first()

    # (c) 旧构建晚完成不覆盖 I2
    async with async_session_factory() as db:
        ob = await index_service.build_index(db, c1)  # 旧构建（ready，未激活）
        ob_id = ob.id
    async with async_session_factory() as db:
        obv = await db.get(IndexVersion, ob_id)
        i2v = await db.get(IndexVersion, i2.id)
        obv.created_at = now() - timedelta(seconds=120)
        i2v.created_at = now()
        await db.commit()
    async with async_session_factory() as db:
        await index_service.activate(db, await db.get(IndexVersion, ob_id))
    async with async_session_factory() as db:
        active_c = (await db.execute(
            select(IndexVersion).where(IndexVersion.course_id == c1, IndexVersion.status == "active")
        )).scalars().first()
        ob_after = await db.get(IndexVersion, ob_id)
    c_ok = active_c.id == i2.id and ob_after.status == "superseded"

    # (d) 切换提交前注入失败 → 无半激活
    async with async_session_factory() as db:
        i3 = await index_service.build_index(db, c1)
        i3_id = i3.id
    async with async_session_factory() as db:
        i3v = await db.get(IndexVersion, i3_id)
        i2v = await db.get(IndexVersion, i2.id)
        # 确保 i3 明确晚于当前 active（i2），走正常切换路径再注入失败
        i3v.created_at = i2v.created_at + timedelta(seconds=10)
        await db.commit()

    def _boom_activate():
        raise RuntimeError("activate interrupted")

    index_module._activate_hook = _boom_activate
    act_failed = False
    try:
        async with async_session_factory() as db:
            await index_service.activate(db, await db.get(IndexVersion, i3_id))
    except RuntimeError:
        act_failed = True
    finally:
        index_module._activate_hook = None
    async with async_session_factory() as db:
        active_d = (await db.execute(
            select(IndexVersion).where(IndexVersion.course_id == c1, IndexVersion.status == "active")
        )).scalars().first()
        i3_after = await db.get(IndexVersion, i3_id)
    d_ok = act_failed and active_d.id == i2.id and i3_after.status == "ready"

    record(
        "P04-12",
        "同源重建失败保留 I1 + 更新期间旧索引服务 + 恢复切 I2 + 旧构建不覆盖 + 切换失败无半激活",
        a_ok and b_ok and b_switch and c_ok and d_ok,
        dict(a_ok=a_ok, b_ok=b_ok, b_switch=b_switch, c_ok=c_ok, d_ok=d_ok,
             status_b=r_status.json()["status"]),
    )


# ── P04-13 消费上阶段索引意向并支持持久恢复、重复交付幂等 ─────────────
async def p04_13(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0413-L1")
    _, v1 = await add_transcript(l1, [_seg(0, "转写内容 P13-ANCHOR", start_ms=0, end_ms=1000)])
    ev_id = await make_transcript_evidence(ScopeContext(subject_id=SUBJECT_A), v1, 0, 0, "转写内容 P13-ANCHOR")
    n1 = await add_published_note(l1, v1, "N1", "笔记内容 NOTE-P13", evidence_ids=[str(ev_id)])

    # 消费意向 → 建索引 + 笔记块入索引
    intent_id = await add_index_intent(n1, v1)
    async with async_session_factory() as db:
        intent = await db.get(IndexIntent, intent_id)
        await index_service.consume_intent(db, intent)
    async with async_session_factory() as db:
        active = (await db.execute(
            select(IndexVersion).where(IndexVersion.course_id == c1, IndexVersion.status == "active")
        )).scalars().first()
        note_blocks = (await db.execute(
            select(IndexBlock).where(
                IndexBlock.index_version_id == active.id, IndexBlock.source_type == "note"
            )
        )).scalars().all()
        consumed = (await db.get(IndexIntent, intent_id)).status == "consumed"
    consume_ok = active is not None and len(note_blocks) == 1 and consumed

    # 重复交付同意向 → 不重复写块
    async with async_session_factory() as db:
        intent2 = await db.get(IndexIntent, intent_id)
        await index_service.consume_intent(db, intent2)
    async with async_session_factory() as db:
        note_count = (await db.execute(
            select(func.count(IndexBlock.id)).where(
                IndexBlock.source_note_version_id == n1
            )
        )).scalar_one()
    no_dup_intent = note_count == 1

    # 恢复：构建首块提交后退出，恢复沿用已提交块，不重复写
    l2, c2 = await create_lecture(SUBJECT_A, "p0413-L2")
    segs = [_seg(i, f"恢复块{i}" + "字" * 800, start_ms=i * 5000, end_ms=(i + 1) * 5000 - 100) for i in range(3)]
    await add_transcript(l2, segs)
    async with async_session_factory() as db:
        job = await index_service.create_index_job(db, ScopeContext(subject_id=SUBJECT_A), c2)
        job_id = job.id

    def _boom():
        raise index_module.BuildInterrupted()

    index_module._build_hook = _boom
    crashed = False
    try:
        async with async_session_factory() as db:
            await index_service.run_index_job(db, job_id)
    except index_module.BuildInterrupted:
        crashed = True
    finally:
        index_module._build_hook = None

    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, job_id)
        iv_partial = await db.get(IndexVersion, j.output_version)
        partial_blocks = (await db.execute(
            select(func.count(IndexBlock.id)).where(IndexBlock.index_version_id == iv_partial.id)
        )).scalar_one()
        partial_persisted = iv_partial.status == "building" and partial_blocks >= 1

    # 恢复（旧 worker 失联：先显式过期租约，模拟失联后租约到期被接管）
    await expire_lease(job_id)
    async with async_session_factory() as db:
        await index_service.run_index_job(db, job_id)
    async with async_session_factory() as db:
        j2 = await db.get(ProcessingJob, job_id)
        total_blocks = (await db.execute(
            select(func.count(IndexBlock.id)).where(IndexBlock.index_version_id == j2.output_version)
        )).scalar_one()
    recover_ok = crashed and partial_persisted and j2.status == "succeeded" and total_blocks == 3

    # 重复执行同 job → 不重复块 / 有效索引
    async with async_session_factory() as db:
        await index_service.run_index_job(db, job_id)
    async with async_session_factory() as db:
        active_count = (await db.execute(
            select(func.count(IndexVersion.id)).where(
                IndexVersion.course_id == c2, IndexVersion.status == "active"
            )
        )).scalar_one()
        block_count = (await db.execute(
            select(func.count(IndexBlock.id)).where(IndexBlock.index_version_id == j2.output_version)
        )).scalar_one()
    rerun_ok = active_count == 1 and block_count == 3

    record(
        "P04-13",
        "意向消费幂等 + 恢复沿用已提交块 + 重复执行不重复产物",
        consume_ok and no_dup_intent and recover_ok and rerun_ok,
        dict(consume_ok=consume_ok, no_dup_intent=no_dup_intent, crashed=crashed,
             partial_persisted=partial_persisted, partial_blocks=partial_blocks,
             total_blocks=total_blocks, rerun_ok=rerun_ok),
    )


# ── P04-14 无证据与检索故障分型，降级不放宽过滤 ────────────────────────
async def p04_14(current, client):
    current["scope"] = None
    l1, c1 = await create_lecture(SUBJECT_A, "p0414-L1")
    l2, _ = await create_lecture(SUBJECT_A, "p0414-L2")
    async with async_session_factory() as db:
        m2 = await db.get(Meeting, l2)
        m2.course_id = c1  # 同课程另一课时：降级仍按课时范围过滤
        await db.commit()
    await add_transcript(l1, [_seg(0, "L1内容 FAULT-L1-ANCHOR", start_ms=0, end_ms=1000)])
    await add_transcript(l2, [_seg(0, "L2内容 FAULT-L2-ANCHOR", start_ms=0, end_ms=1000)])
    await build_active_index(c1)

    # 无相关内容 → no_evidence
    r_no = await search(client, "完全不存在的术语", course_id=c1)
    no_evidence = r_no.json()["status"] == "no_evidence"

    # 向量失败 + 词法可用 → degraded，仍保留范围过滤
    retrieval_module.VECTOR_FAIL = True
    try:
        r_deg = await search(client, "FAULT-L1-ANCHOR", course_id=c1, lecture_id=l1)
        # 指定 L1 范围，L2 的 FAULT-L2-ANCHOR 不应召回
        r_deg_scope = await search(client, "FAULT-L2-ANCHOR", course_id=c1, lecture_id=l1)
    finally:
        retrieval_module.VECTOR_FAIL = False
    deg = r_deg.json()
    degraded_ok = (
        deg["status"] == "degraded" and "lexical" in deg["trace"]["branches"]
        and "vector" not in deg["trace"]["branches"] and deg["total"] >= 1
        and r_deg_scope.json()["status"] == "no_evidence"
    )

    # embedding 失败 → 向量分支不可用 → degraded
    retrieval_module.EMBED_FAIL = True
    try:
        r_emb = await search(client, "FAULT-L1-ANCHOR", course_id=c1)
    finally:
        retrieval_module.EMBED_FAIL = False
    embed_fail_ok = r_emb.json()["status"] == "degraded" and r_emb.json()["total"] >= 1

    # 两路均失败 → retrieval_error（不是空成功）
    retrieval_module.VECTOR_FAIL = True
    retrieval_module.LEXICAL_FAIL = True
    try:
        r_err = await search(client, "FAULT-L1-ANCHOR", course_id=c1)
    finally:
        retrieval_module.VECTOR_FAIL = False
        retrieval_module.LEXICAL_FAIL = False
    both_fail_ok = (
        r_err.json()["status"] == "retrieval_error" and r_err.json()["total"] == 0
    )

    record(
        "P04-14",
        "no_evidence / degraded（记录分支）/ retrieval_error 分型 + 降级仍按范围过滤",
        no_evidence and degraded_ok and embed_fail_ok and both_fail_ok,
        dict(no_evidence=no_evidence, degraded_status=deg["status"],
             degraded_trace=deg["trace"], degraded_ok=degraded_ok,
             embed_fail_ok=embed_fail_ok, both_fail_ok=both_fail_ok),
    )


# ── P04-15 最小检索页面能打开正确来源，切课程不串结果（静态前端烟测） ───
async def p04_15(current, client):
    fe_dir = ROOT.parent / "frontend" / "src"
    page = fe_dir / "features" / "retrieval" / "pages" / "retrieval-page.tsx"
    api_file = fe_dir / "features" / "retrieval" / "api.ts"
    routes_file = fe_dir / "router" / "routes.tsx"
    sidebar = fe_dir / "components" / "layout" / "sidebar.tsx"
    page_src = page.read_text(encoding="utf-8") if page.exists() else ""
    api_src = api_file.read_text(encoding="utf-8") if api_file.exists() else ""
    routes_src = routes_file.read_text(encoding="utf-8") if routes_file.exists() else ""
    sidebar_src = sidebar.read_text(encoding="utf-8") if sidebar.exists() else ""

    fe_ok = (
        page.exists()
        # 范围与来源版本显式显示
        and "courseId" in page_src and "lectureId" in page_src
        # 切课程清空结果，不串缓存
        and "setResponse(null)" in page_src
        # 文档打开真实定位（页码/块）
        and "getDocumentBlocks" in page_src and "page_no" in page_src
        # 转写按固定资产定位回放，缺时间只定位原文
        and "source_audio_asset_id" in page_src and "/api/audio-assets/" in page_src
        and "onLoadedMetadata" in page_src and "currentTime" in page_src and ".play()" in page_src
        # 旧版本标记
        and "is_current" in page_src
        and "retrieval" in routes_src and "检索" in sidebar_src
        and "retrieval/search" in api_src
    )

    record(
        "P04-15",
        "最小页面含范围/版本/来源打开/固定资产回放逻辑 + 切课程清结果（静态烟测；浏览器实测见 verify_phase4_retrieval_browser.cjs）",
        fe_ok,
        dict(page=page.exists(), routes=("retrieval" in routes_src),
             sidebar=("检索" in sidebar_src),
             scope=("courseId" in page_src and "lectureId" in page_src),
             clear_cache=("setResponse(null)" in page_src),
             doc_open=("getDocumentBlocks" in page_src),
             fixed_audio=("source_audio_asset_id" in page_src and "/api/audio-assets/" in page_src),
             playback=("onLoadedMetadata" in page_src and "currentTime" in page_src),
             api=("retrieval/search" in api_src)),
    )


# ── P04-16 第三阶段升级与既有数据保留，验收证据可复跑 ───────────────────
async def p04_16():
    # 路径 1：主库空库 → head
    conn = await asyncpg.connect(pg_dsn(MAIN_DB))
    try:
        main_head = await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()

    # 路径 2：阶段三收口 head 含音频/转写/笔记/evidence/IndexIntent 副本 → head 保留数据
    db2 = PREFIX + "p3data"
    DBS.append(db2)
    await create_db(db2)
    run_alembic("b3c4d5e6f7a8", asyncpg_dsn(db2))
    cid, mid, aid, vid, sid, eid, nid, iid, kid = (uuid.uuid4() for _ in range(9))
    conn = await asyncpg.connect(pg_dsn(db2))
    try:
        await conn.execute(
            "INSERT INTO courses (id, owner_id, title, created_at, updated_at) VALUES ($1,$2,$3,now(),now())",
            cid, SUBJECT_A, "p3-course",
        )
        await conn.execute(
            "INSERT INTO meetings (id, course_id, title, status, legacy_meeting) VALUES ($1,$2,$3,'processed',false)",
            mid, cid, "p3-lecture",
        )
        await conn.execute(
            "INSERT INTO audio_assets (id, lecture_id, storage_key, version, is_demo, created_at) VALUES ($1,$2,$3,1,false,now())",
            aid, mid, "assets/old.wav",
        )
        await conn.execute(
            "INSERT INTO transcript_versions (id, audio_asset_id, lecture_id, version, provider, status, created_at) VALUES ($1,$2,$3,1,'real','active',now())",
            vid, aid, mid,
        )
        await conn.execute(
            "INSERT INTO transcript_segments (id, version_id, seq_index, speaker, content, start_ms, end_ms, created_at) VALUES ($1,$2,0,'a','旧片段',0,1000,now())",
            sid, vid,
        )
        await conn.execute(
            "INSERT INTO evidence_spans (id, transcript_version_id, segment_start, segment_end, is_valid, created_at) VALUES ($1,$2,0,0,true,now())",
            eid, vid,
        )
        await conn.execute(
            "INSERT INTO note_versions (id, lecture_id, transcript_version_id, status, content, created_at) VALUES ($1,$2,$3,'published','{\"title\":\"旧笔记\"}'::jsonb,now())",
            nid, mid, vid,
        )
        await conn.execute(
            "INSERT INTO index_intents (id, note_version_id, source_version_id, status, created_at) VALUES ($1,$2,$3,'pending',now())",
            iid, nid, vid,
        )
        await conn.execute(
            "INSERT INTO knowledge_documents (id, title, owner_id, source_type, content, created_at) VALUES ($1,$2,$3,'uploaded_doc','旧知识',now())",
            kid, "旧知识", SUBJECT_A,
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
            await conn.fetchval("SELECT count(*) FROM note_versions WHERE id = $1", nid),
            await conn.fetchval("SELECT count(*) FROM index_intents WHERE id = $1", iid),
            await conn.fetchval("SELECT count(*) FROM knowledge_documents WHERE id = $1", kid),
        )
        all_kept = all(k == 1 for k in kept)
        new_tables = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' "
            "AND table_name IN ('parsed_blocks','index_versions','index_blocks')"
        )
        new_cols = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE "
            "(table_name='source_documents' AND column_name IN ('filename','content_hash')) "
            "OR (table_name='source_document_versions' AND column_name IN ('parse_status','error_code','content_hash','config','block_count'))"
        )
        # 旧未归属课堂知识记录保持原样（不被清空/合并）
        knowledge_kept = await conn.fetchval(
            "SELECT count(*) FROM knowledge_documents WHERE id = $1 AND owner_id = $2", kid, SUBJECT_A
        )
    finally:
        await conn.close()

    record(
        "P04-16",
        "空库与阶段三 head 均升级到 phase4 head + 旧 ID/引用/资产保留 + 新结构就位",
        main_head == "c4d5e6f7a8b9" and head == "c4d5e6f7a8b9"
        and all_kept and new_tables == 3 and new_cols == 7 and knowledge_kept == 1,
        dict(main_head=main_head, head=head, kept=kept, new_tables=new_tables,
             new_cols=new_cols, knowledge_kept=knowledge_kept),
    )


if __name__ == "__main__":
    asyncio.run(main())
