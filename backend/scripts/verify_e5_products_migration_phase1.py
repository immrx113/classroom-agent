r"""阶段一·原 e5 已存在合法/非法产物及引用的补偿迁移验证（F2 / F3）

模拟一条已在 e5f6a7b8c9d0 状态下运行过的库：已经存在合法的
transcript_version / transcript_segments / evidence_spans / artifact_evidence，
同时混入非法产物（负时间、倒置时间、缺片段范围、文档证据缺 block_id、白名单外产物类型）。

从 e5 出发 ``alembic upgrade head``（d5e6f7a8b9c0 + e6f7a8b9c0d1），验证：

- F2  补偿迁移不再无条件 DELETE：合法产物（版本/片段/证据/关联）全部保留，记录数不变
- F2  回填幂等：已存在 transcript_version 的课时不会重复建版本（旧 transcripts 不触发二次回填）
- F3  非法证据隔离：缺/倒置片段范围 → is_valid=FALSE + 解除来源；文档缺 block_id → 隔离
- F3  负时间 / 倒置时间 → 清空为 NULL（不写非法值，保留记录）
- F3  白名单外产物类型 → 仅删除链接行
- 引用可解析性：合法证据仍指向既有版本，合法关联仍指向既有产物与证据
- e6  追加 owner_id 归属列并收紧 NOT NULL

运行（在 backend 目录）：
  cd backend
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  $env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/yuan_meet_classroom_e5products_test"
  python scripts/verify_e5_products_migration_phase1.py
"""

import asyncio
import sys
import uuid

import asyncpg
from alembic import command
from alembic.config import Config

from app.config import settings

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def _target_db() -> str:
    db = settings.DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in db:
        print(f"REFUSE: DATABASE_URL 指向非测试库 '{db}'（库名需包含 test）")
        sys.exit(2)
    return db


def _target_dsn() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


def _maint_dsn() -> str:
    base = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + "/postgres"


def run_alembic(revision: str) -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, revision)


async def _reset_and_create(dbname: str) -> None:
    conn = await asyncpg.connect(_maint_dsn())
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await conn.close()

    conn = await asyncpg.connect(_target_dsn())
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()


async def _seed() -> dict:
    """在 e5 状态塞入合法 + 非法产物，返回各主键以便验证"""
    conn = await asyncpg.connect(_target_dsn())
    try:
        ids: dict = {}
        course = uuid.uuid4()
        meeting = uuid.uuid4()
        audio = uuid.uuid4()
        tv = uuid.uuid4()
        seg0 = uuid.uuid4()
        seg1 = uuid.uuid4()
        doc = uuid.uuid4()
        docv = uuid.uuid4()
        note = uuid.uuid4()

        await conn.execute(
            "INSERT INTO courses (id, owner_id, title, term) "
            "VALUES ($1::uuid, 'local-user', '产品课程', '2026秋')",
            course,
        )
        await conn.execute(
            "INSERT INTO meetings (id, course_id, title, status, transcription_mode) "
            "VALUES ($1::uuid, $2::uuid, '产品会议', 'completed', 'real')",
            meeting, course,
        )
        await conn.execute(
            "INSERT INTO audio_assets (id, lecture_id, storage_key, version, is_demo) "
            "VALUES ($1::uuid, $2::uuid, 'oss://prod/a.mp3', 1, FALSE)",
            audio, meeting,
        )
        await conn.execute(
            "INSERT INTO transcript_versions (id, audio_asset_id, lecture_id, version, status) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, 1, 'active')",
            tv, audio, meeting,
        )
        for sid, seq, c, s, e in [
            (seg0, 0, "片段A", 0, 1000),
            (seg1, 1, "片段B", 1000, 2000),
        ]:
            await conn.execute(
                "INSERT INTO transcript_segments "
                "(id, version_id, seq_index, content, start_ms, end_ms) "
                "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)",
                sid, tv, seq, c, s, e,
            )
        await conn.execute(
            "INSERT INTO source_documents (id, course_id, title) "
            "VALUES ($1::uuid, $2::uuid, '课件')",
            doc, course,
        )
        await conn.execute(
            "INSERT INTO source_document_versions (id, source_document_id, version, status) "
            "VALUES ($1::uuid, $2::uuid, 1, 'active')",
            docv, doc,
        )
        await conn.execute(
            "INSERT INTO note_versions (id, lecture_id, status, content) "
            "VALUES ($1::uuid, $2::uuid, 'draft', '{}'::jsonb)",
            note, meeting,
        )

        # 旧转写（幂等：meeting 已有 version，回填应跳过，不产生第二份版本）
        await conn.execute(
            "INSERT INTO transcripts (id, meeting_id, speaker, content, start_time, end_time, seq_index) "
            "VALUES ($1::uuid, $2::uuid, '老师', '旧转写', 0.0, 1.0, 0)",
            uuid.uuid4(), meeting,
        )

        # 证据：合法 + 非法
        e_valid_tx = uuid.uuid4()   # 合法转写证据
        e_valid_doc = uuid.uuid4()  # 合法文档证据
        e_neg_time = uuid.uuid4()   # 负时间
        e_inv_time = uuid.uuid4()   # 倒置时间
        e_null_seg = uuid.uuid4()   # 缺片段范围（转写）
        e_null_block = uuid.uuid4()  # 缺 block_id（文档）

        await conn.execute(
            "INSERT INTO evidence_spans "
            "(id, transcript_version_id, segment_start, segment_end, start_ms, end_ms, is_valid) "
            "VALUES ($1::uuid, $2::uuid, 0, 1, 0, 2000, TRUE)",
            e_valid_tx, tv,
        )
        await conn.execute(
            "INSERT INTO evidence_spans (id, document_version_id, block_id, is_valid) "
            "VALUES ($1::uuid, $2::uuid, 'b1', TRUE)",
            e_valid_doc, docv,
        )
        await conn.execute(
            "INSERT INTO evidence_spans "
            "(id, transcript_version_id, segment_start, segment_end, start_ms, end_ms, is_valid) "
            "VALUES ($1::uuid, $2::uuid, 0, 1, -5, 10, TRUE)",
            e_neg_time, tv,
        )
        await conn.execute(
            "INSERT INTO evidence_spans "
            "(id, transcript_version_id, segment_start, segment_end, start_ms, end_ms, is_valid) "
            "VALUES ($1::uuid, $2::uuid, 0, 1, 100, 50, TRUE)",
            e_inv_time, tv,
        )
        await conn.execute(
            "INSERT INTO evidence_spans (id, transcript_version_id, segment_start, segment_end, is_valid) "
            "VALUES ($1::uuid, $2::uuid, NULL, NULL, TRUE)",
            e_null_seg, tv,
        )
        await conn.execute(
            "INSERT INTO evidence_spans (id, document_version_id, block_id, is_valid) "
            "VALUES ($1::uuid, $2::uuid, NULL, TRUE)",
            e_null_block, docv,
        )

        # 产物关联：合法 + 白名单外
        await conn.execute(
            "INSERT INTO artifact_evidence (id, artifact_type, artifact_id, evidence_id) "
            "VALUES ($1::uuid, 'note_version', $2::uuid, $3::uuid)",
            uuid.uuid4(), note, e_valid_tx,
        )
        await conn.execute(
            "INSERT INTO artifact_evidence (id, artifact_type, artifact_id, evidence_id) "
            "VALUES ($1::uuid, 'bogus', $2::uuid, $3::uuid)",
            uuid.uuid4(), uuid.uuid4(), e_valid_tx,
        )

        ids.update(
            course=course, meeting=meeting, audio=audio, tv=tv,
            seg0=seg0, seg1=seg1, doc=doc, docv=docv, note=note,
            e_valid_tx=e_valid_tx, e_valid_doc=e_valid_doc,
            e_neg_time=e_neg_time, e_inv_time=e_inv_time,
            e_null_seg=e_null_seg, e_null_block=e_null_block,
        )
        return ids
    finally:
        await conn.close()


async def _verify(ids: dict) -> None:
    conn = await asyncpg.connect(_target_dsn())
    try:
        # F2 合法产物保留（记录数不变）
        tv_count = await conn.fetchval("SELECT count(*) FROM transcript_versions")
        seg_count = await conn.fetchval("SELECT count(*) FROM transcript_segments")
        audio_count = await conn.fetchval("SELECT count(*) FROM audio_assets")
        ev_count = await conn.fetchval("SELECT count(*) FROM evidence_spans")
        record("F2 版本/片段/音频/证据 记录数保留", (tv_count, seg_count, audio_count, ev_count) == (1, 2, 1, 6),
               f"tv={tv_count} seg={seg_count} audio={audio_count} ev={ev_count}")

        # F2 回填幂等：旧 transcripts 未触发二次建版本
        record("F2 回填幂等(既有版本跳过)", tv_count == 1, f"tv={tv_count}")

        # F3 非法证据隔离
        ns = await conn.fetchrow(
            "SELECT is_valid, transcript_version_id, document_version_id FROM evidence_spans WHERE id = $1::uuid",
            ids["e_null_seg"],
        )
        record("F3 缺片段范围证据隔离", ns["is_valid"] is False and ns["transcript_version_id"] is None,
               f"is_valid={ns['is_valid']} tx={ns['transcript_version_id']}")
        nb = await conn.fetchrow(
            "SELECT is_valid, document_version_id FROM evidence_spans WHERE id = $1::uuid",
            ids["e_null_block"],
        )
        record("F3 文档缺 block_id 证据隔离", nb["is_valid"] is False and nb["document_version_id"] is None,
               f"is_valid={nb['is_valid']} doc={nb['document_version_id']}")

        # F3 负时间清空（保留记录，不清来源）
        nt = await conn.fetchrow(
            "SELECT start_ms, end_ms, is_valid FROM evidence_spans WHERE id = $1::uuid",
            ids["e_neg_time"],
        )
        record("F3 负时间清空", nt["start_ms"] is None and nt["end_ms"] == 10 and nt["is_valid"] is True,
               f"start_ms={nt['start_ms']} end_ms={nt['end_ms']}")
        it = await conn.fetchrow(
            "SELECT start_ms, end_ms FROM evidence_spans WHERE id = $1::uuid",
            ids["e_inv_time"],
        )
        record("F3 倒置时间成对清空", it["start_ms"] is None and it["end_ms"] is None,
               f"start_ms={it['start_ms']} end_ms={it['end_ms']}")

        # 合法证据保留 + 引用可解析
        vt = await conn.fetchrow(
            "SELECT is_valid, transcript_version_id FROM evidence_spans WHERE id = $1::uuid",
            ids["e_valid_tx"],
        )
        record("F2 合法转写证据保留 + 引用可解析",
               vt["is_valid"] is True and vt["transcript_version_id"] == ids["tv"],
               f"is_valid={vt['is_valid']} tx={vt['transcript_version_id']}")
        vd = await conn.fetchrow(
            "SELECT is_valid, document_version_id FROM evidence_spans WHERE id = $1::uuid",
            ids["e_valid_doc"],
        )
        record("F2 合法文档证据保留 + 引用可解析",
               vd["is_valid"] is True and vd["document_version_id"] == ids["docv"],
               f"is_valid={vd['is_valid']} doc={vd['document_version_id']}")

        # F3 白名单外产物类型删除（仅链接行）
        ae_rows = await conn.fetch("SELECT artifact_type FROM artifact_evidence")
        types = sorted(r["artifact_type"] for r in ae_rows)
        record("F3 白名单外产物类型删除", types == ["note_version"], f"got {types}")

        # 合法关联保留 + 引用可解析
        ae_valid = await conn.fetchrow(
            "SELECT artifact_id, evidence_id FROM artifact_evidence WHERE artifact_type = 'note_version'"
        )
        record("F2 合法产物关联保留 + 引用可解析",
               ae_valid["artifact_id"] == ids["note"] and ae_valid["evidence_id"] == ids["e_valid_tx"],
               f"artifact={ae_valid['artifact_id']} evidence={ae_valid['evidence_id']}")

        # e6 归属列存在 + NOT NULL
        for tbl in ("chat_sessions", "knowledge_documents"):
            col = await conn.fetchrow(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'owner_id'",
                tbl,
            )
            record(f"e6 {tbl}.owner_id NOT NULL", col is not None and col["is_nullable"] == "NO",
                   f"got {col['is_nullable'] if col else None}")

        # 约束存在（含放宽后的单来源约束）
        for cname in (
            "ck_evidence_span_single_source",
            "ck_evidence_span_transcript_span",
            "ck_evidence_span_document_block",
            "ck_evidence_span_start_nonneg",
            "ck_evidence_span_end_nonneg",
            "ck_evidence_span_time_order",
            "ck_transcript_segment_start_nonneg",
            "ck_transcript_segment_end_nonneg",
            "ck_transcript_segment_time_order",
            "ck_artifact_evidence_type",
        ):
            exists = await conn.fetchval(
                "SELECT count(*) FROM pg_constraint WHERE conname = $1", cname
            )
            record(f"F3 约束 {cname}", exists == 1, f"count={exists}")
    finally:
        await conn.close()


def main() -> None:
    db = _target_db()
    print(f"=== 原 e5 已存在合法/非法产物补偿迁移验证（目标库 {db}）===")

    print("[1/4] 重建测试库 + vector 扩展")
    asyncio.run(_reset_and_create(db))

    print("[2/4] 迁移到 e5f6a7b8c9d0 并塞入合法/非法产物")
    run_alembic("e5f6a7b8c9d0")
    ids = asyncio.run(_seed())

    print("[3/4] 迁移到 head（d5 补偿 + e6 归属）")
    run_alembic("head")

    print("[4/4] 验证补偿结果")
    asyncio.run(_verify(ids))

    failed = [r for r in RESULTS if not r[1]]
    print("=" * 60)
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        extra = f"  ({detail})" if detail and not ok else ""
        print(f"[{mark}] {name}{extra}")
    print("-" * 60)
    print(f"TOTAL {len(RESULTS)}  PASS {len(RESULTS) - len(failed)}  FAIL {len(failed)}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
