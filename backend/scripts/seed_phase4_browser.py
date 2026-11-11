"""阶段 4「问答/检索闭环」浏览器证据的种子数据（真实本地库 + 真实可解码音频）。

仅写独立库 ``yuan_resetv1_p4_browser``，绝不触碰活跃库 ``yuan_classroom_local`` 或正式库。
确定性 embedding，不调用任何收费模型。为浏览器「点击来源跳转」准备：
- 转写来源：真实可解码 WAV 资产（16kHz 单声道，8 秒），segment 带 start_ms=3000。
- 课件来源：第 1 页含专属标记 DOCSTACK_PAGE1 的已解析块。
- 笔记来源：已发布笔记，含结构化例题 EXAMPLECALC_3PLUS2。

运行（backend 目录）：
  .venv/Scripts/python.exe scripts/seed_phase4_browser.py
"""

import asyncio
import json
import math
import os
import struct
import subprocess
import sys
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.update(
    OPENAI_API_KEY="", DASHSCOPE_API_KEY="", PYTHONPATH=".", PYTHONUTF8="1"
)

import asyncpg
from app.config import settings

ROOT = Path(__file__).resolve().parents[1]  # backend/
DB_NAME = "yuan_resetv1_p4_browser"
STATE_PATH = ROOT / "runtime" / "phase4-browser-state.json"

BASE = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]

# 标记词（与 verify_resetv1_phase4.py 保持一致，确保「引用对应原文」可核对）
MARK_DOC = "DOCSTACK_PAGE1"
MARK_EXAMPLE = "EXAMPLECALC_3PLUS2"
MARK_TRANS = "LINMARK_ALPHA"


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
    """生成可解码单声道 16-bit PCM WAV，返回字节数。"""
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


def _seg(seq, content, start_ms=None, end_ms=None) -> dict:
    return dict(seq_index=seq, speaker="t", content=content, start_ms=start_ms, end_ms=end_ms)


async def seed() -> None:
    from app.db.session import async_session_factory
    from app.models.audio_asset import AudioAsset
    from app.models.course import Course
    from app.models.meeting import Meeting
    from app.models.note_version import NoteVersion
    from app.models.parsed_block import ParsedBlock
    from app.models.source_document import SourceDocument, SourceDocumentVersion
    from app.models.transcript_version import TranscriptSegment, TranscriptVersion
    from app.services.embedding_service import DeterministicEmbedding
    from app.services.index_service import index_service

    owner = settings.LOCAL_SUBJECT_ID

    async with async_session_factory() as db:
        course = Course(owner_id=owner, title="阶段四浏览器验收课程", teacher="验收", term="2026秋")
        db.add(course)
        await db.flush()
        meeting = Meeting(course_id=course.id, title="第 1 讲 数据结构", status="pending")
        db.add(meeting)
        await db.flush()
        cid, lid = course.id, meeting.id
        await db.commit()

    # ── 转写 + 真实音频资产 ──────────────────────────────────────────
    asset_id = uuid.uuid4()
    storage_key = f"assets/{asset_id}.wav"
    upload_dir = Path(settings.UPLOAD_DIR).resolve()
    wav_path = upload_dir / storage_key
    wav_size = write_wav(wav_path, seconds=8.0)
    start_ms, end_ms = 3000, 5000

    async with async_session_factory() as db:
        meeting = await db.get(Meeting, lid)
        asset = AudioAsset(
            id=asset_id, lecture_id=lid, storage_key=storage_key,
            checksum=f"sha256-phase4-browser-{asset_id.hex[:8]}",
            mime="audio/wav", duration_ms=8000, size=wav_size, version=1, is_demo=False,
        )
        db.add(asset)
        await db.flush()
        version = TranscriptVersion(
            id=uuid.uuid4(), audio_asset_id=asset.id, lecture_id=lid, version=1,
            provider="fake", status="active",
        )
        db.add(version)
        await db.flush()
        for s in [
            _seg(0, f"线性表定义讲解 {MARK_TRANS} 顺序存储结构", start_ms=start_ms, end_ms=end_ms),
            _seg(1, "本课时继续讲解栈与队列的抽象数据类型", start_ms=5000, end_ms=7000),
        ]:
            db.add(TranscriptSegment(
                version_id=version.id, seq_index=s["seq_index"], speaker=s["speaker"],
                content=s["content"], start_ms=s["start_ms"], end_ms=s["end_ms"],
            ))
        meeting.active_audio_id = asset.id
        meeting.active_transcript_version_id = version.id
        meeting.status = "processed"
        await db.commit()
        transcript_version_id = version.id

    # ── 课件（已解析，第 1 页含标记） ────────────────────────────────
    async with async_session_factory() as db:
        doc = SourceDocument(
            course_id=cid, lecture_id=lid, title="阶段四课件", filename="phase4-doc.pdf",
            storage_key="docs/phase4-doc.pdf", mime="application/pdf", content_hash="hash-phase4-doc",
        )
        db.add(doc)
        await db.flush()
        ver = SourceDocumentVersion(
            source_document_id=doc.id, version=1, status="active",
            parse_status="parsed", block_count=2,
        )
        db.add(ver)
        await db.flush()
        blocks = [
            {"block_id": "p1", "page_no": 1, "text": f"栈的抽象数据类型 {MARK_DOC} 入栈与出栈操作", "kind": "text"},
            {"block_id": "p2", "page_no": 2, "text": "队列先进先出 FIFO", "kind": "text"},
        ]
        for i, b in enumerate(blocks):
            db.add(ParsedBlock(
                document_version_id=ver.id, block_id=b["block_id"], seq=i,
                kind=b["kind"], text=b["text"], heading=None, page_no=b["page_no"],
                paragraph_index=None, table_index=None,
            ))
        await db.commit()
        document_id = doc.id

    # ── 已发布笔记（结构化例题，confirmed） ──────────────────────────
    async with async_session_factory() as db:
        meeting = await db.get(Meeting, lid)
        note = NoteVersion(
            lecture_id=lid, status="published",
            content={
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
            },
            published_at=datetime.now(timezone.utc),
        )
        db.add(note)
        await db.flush()
        meeting.active_note_version_id = note.id
        await db.commit()
        note_id = note.id

    # ── 建索引 + 激活 ────────────────────────────────────────────────
    async with async_session_factory() as db:
        idx = await index_service.build_index(db, cid, embed=DeterministicEmbedding(1024))
        await index_service.activate(db, idx)

    state = {
        "db": DB_NAME,
        "course_id": str(cid),
        "lecture_id": str(lid),
        "note_id": str(note_id),
        "document_id": str(document_id),
        "audio_asset_id": str(asset_id),
        "transcript_version_id": str(transcript_version_id),
        "markers": {"document": MARK_DOC, "example": MARK_EXAMPLE, "transcript": MARK_TRANS},
        "expect": {
            "document_page": 1,
            "transcript_start_ms": start_ms,
            "transcript_end_ms": end_ms,
            "example_text": f"例题：{MARK_EXAMPLE} 计算 2+3",
        },
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))


async def main() -> None:
    await create_db(DB_NAME)
    run_alembic("head", asyncpg_dsn(DB_NAME))
    # 把引擎指向种子库（在导入会创建引擎的模块之前）
    settings.DATABASE_URL = asyncpg_dsn(DB_NAME)
    os.environ["DATABASE_URL"] = settings.DATABASE_URL
    settings.INDEX_EMBEDDING_PROVIDER = "deterministic"
    await seed()
    from app.db.session import engine
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
