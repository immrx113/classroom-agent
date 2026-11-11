r"""阶段一·第二轮补偿迁移验证（G1 / G2）

模拟一条已经应用过「原版 d5」的库：
- G1  单来源约束仍是 e5 遗留的严格形式
      ``(transcript_version_id IS NOT NULL) != (document_version_id IS NOT NULL)``；
- G2  原版回填遗留的错误产物：演示 / 未知标记、版本状态、活动指针、旧片段映射均错误。

从「原版 d5 状态」出发 ``alembic upgrade head``（e6 + f7a8b9c0d1e2），验证：

- G1  单来源约束被放宽，最终定义与 fresh 路径一致（含 ``is_valid`` 分支）；
- G2  可信来源（real）的演示标记 / 版本状态 / 活动指针 / 旧片段映射被修复；
- G2  未知来源（NULL）被隔离：演示标记置真、版本状态 ready、活动指针清空；
- G2  既有合法证据 ID 不变、仍有效、仍指向既有版本；版本不重建、不删除。

运行（在 backend 目录）：
  cd backend
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  $env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/yuan_meet_classroom_round2_test"
  python scripts/verify_round2_phase1.py
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


async def _revert_to_old_d5_and_seed() -> dict:
    """把已到 d5 的空库改回「原版 d5」状态：严格约束 + 错误回填产物。"""
    conn = await asyncpg.connect(_target_dsn())
    try:
        # G1 前置：单来源约束回退为 e5 遗留的严格形式
        await conn.execute(
            "ALTER TABLE evidence_spans DROP CONSTRAINT ck_evidence_span_single_source"
        )
        await conn.execute(
            "ALTER TABLE evidence_spans ADD CONSTRAINT ck_evidence_span_single_source "
            "CHECK ((transcript_version_id IS NOT NULL) != (document_version_id IS NOT NULL))"
        )

        ids: dict = {}
        course = uuid.uuid4()
        await conn.execute(
            "INSERT INTO courses (id, owner_id, title, term) "
            "VALUES ($1::uuid, 'local-user', 'G2课程', '2026秋')",
            course,
        )

        # ── 可信来源（real）：演示标记/状态/指针/映射全部错误 ──
        mid_real = uuid.uuid4()
        aud_real = uuid.uuid4()
        ver_real = uuid.uuid4()
        seg_real = uuid.uuid4()
        ev_real = uuid.uuid4()
        old_t1 = uuid.uuid4()

        await conn.execute(
            "INSERT INTO meetings (id, course_id, title, status, transcription_mode, audio_url) "
            "VALUES ($1::uuid, $2::uuid, '可信课时', 'completed', 'real', 'oss://real/a.mp3')",
            mid_real, course,
        )
        await conn.execute(
            "INSERT INTO transcripts (id, meeting_id, speaker, content, start_time, end_time, seq_index) "
            "VALUES ($1::uuid, $2::uuid, '老师', '正文A', 0.0, 1.0, 0)",
            old_t1, mid_real,
        )
        await conn.execute(
            "INSERT INTO audio_assets (id, lecture_id, storage_key, version, is_demo) "
            "VALUES ($1::uuid, $2::uuid, 'oss://real/a.mp3', 1, TRUE)",
            aud_real, mid_real,
        )
        await conn.execute(
            "INSERT INTO transcript_versions (id, audio_asset_id, lecture_id, version, status) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, 1, 'ready')",
            ver_real, aud_real, mid_real,
        )
        await conn.execute(
            "INSERT INTO transcript_segments "
            "(id, version_id, seq_index, content, start_ms, end_ms, source_transcript_id) "
            "VALUES ($1::uuid, $2::uuid, 0, '正文A', 0, 1000, NULL)",
            seg_real, ver_real,
        )
        await conn.execute(
            "INSERT INTO evidence_spans "
            "(id, transcript_version_id, segment_start, segment_end, start_ms, end_ms, is_valid) "
            "VALUES ($1::uuid, $2::uuid, 0, 0, 0, 1000, TRUE)",
            ev_real, ver_real,
        )

        # ── 未知来源（NULL）：演示标记/状态/指针全部错误 ──
        mid_unknown = uuid.uuid4()
        aud_unk = uuid.uuid4()
        ver_unk = uuid.uuid4()
        seg_unk = uuid.uuid4()
        old_t2 = uuid.uuid4()

        await conn.execute(
            "INSERT INTO meetings "
            "(id, course_id, title, status, transcription_mode, active_audio_id, active_transcript_version_id) "
            "VALUES ($1::uuid, $2::uuid, '未知课时', 'pending', NULL, $3::uuid, $4::uuid)",
            mid_unknown, course, aud_unk, ver_unk,
        )
        await conn.execute(
            "INSERT INTO transcripts (id, meeting_id, speaker, content, seq_index) "
            "VALUES ($1::uuid, $2::uuid, '老师', '正文B', 0)",
            old_t2, mid_unknown,
        )
        await conn.execute(
            "INSERT INTO audio_assets (id, lecture_id, storage_key, version, is_demo) "
            "VALUES ($1::uuid, $2::uuid, NULL, 1, FALSE)",
            aud_unk, mid_unknown,
        )
        await conn.execute(
            "INSERT INTO transcript_versions (id, audio_asset_id, lecture_id, version, status) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, 1, 'active')",
            ver_unk, aud_unk, mid_unknown,
        )
        await conn.execute(
            "INSERT INTO transcript_segments "
            "(id, version_id, seq_index, content, source_transcript_id) "
            "VALUES ($1::uuid, $2::uuid, 0, '正文B', NULL)",
            seg_unk, ver_unk,
        )

        ids.update(
            course=course,
            mid_real=mid_real, aud_real=aud_real, ver_real=ver_real,
            seg_real=seg_real, ev_real=ev_real, old_t1=old_t1,
            mid_unknown=mid_unknown, aud_unk=aud_unk, ver_unk=ver_unk,
            seg_unk=seg_unk, old_t2=old_t2,
        )
        return ids
    finally:
        await conn.close()


async def _verify(ids: dict) -> None:
    conn = await asyncpg.connect(_target_dsn())
    try:
        head = await conn.fetchval("SELECT version_num FROM alembic_version")
        record("迁移到达 head", head == "f7a8b9c0d1e2", f"got {head}")

        # G1：单来源约束放宽为与 fresh 一致的形式
        condef = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_evidence_span_single_source'"
        )
        record(
            "G1 单来源约束放宽为隔离兼容形式",
            condef is not None and "is_valid" in condef,
            f"def={condef}",
        )

        # G2 可信来源：演示标记 / 版本状态 / 活动指针 / 旧片段映射 修复
        ademo = await conn.fetchval(
            "SELECT is_demo FROM audio_assets WHERE id = $1::uuid", ids["aud_real"]
        )
        vstatus = await conn.fetchval(
            "SELECT status FROM transcript_versions WHERE id = $1::uuid", ids["ver_real"]
        )
        mptr = await conn.fetchrow(
            "SELECT active_audio_id, active_transcript_version_id FROM meetings WHERE id = $1::uuid",
            ids["mid_real"],
        )
        smap = await conn.fetchval(
            "SELECT source_transcript_id FROM transcript_segments WHERE id = $1::uuid",
            ids["seg_real"],
        )
        record(
            "G2 可信来源演示/状态/指针/映射修复",
            ademo is False
            and vstatus == "active"
            and mptr["active_audio_id"] == ids["aud_real"]
            and mptr["active_transcript_version_id"] == ids["ver_real"]
            and smap == ids["old_t1"],
            f"is_demo={ademo} status={vstatus} ptr=({mptr['active_audio_id']},{mptr['active_transcript_version_id']}) map={smap}",
        )

        # G2 未知来源：演示标记置真 / 版本状态 ready / 活动指针清空
        udemo = await conn.fetchval(
            "SELECT is_demo FROM audio_assets WHERE id = $1::uuid", ids["aud_unk"]
        )
        ustatus = await conn.fetchval(
            "SELECT status FROM transcript_versions WHERE id = $1::uuid", ids["ver_unk"]
        )
        umap = await conn.fetchval(
            "SELECT source_transcript_id FROM transcript_segments WHERE id = $1::uuid",
            ids["seg_unk"],
        )
        uptr = await conn.fetchrow(
            "SELECT active_audio_id, active_transcript_version_id FROM meetings WHERE id = $1::uuid",
            ids["mid_unknown"],
        )
        record(
            "G2 未知来源隔离(演示/ready/清空指针)",
            udemo is True
            and ustatus == "ready"
            and uptr["active_audio_id"] is None
            and uptr["active_transcript_version_id"] is None
            and umap == ids["old_t2"],
            f"is_demo={udemo} status={ustatus} ptr=({uptr['active_audio_id']},{uptr['active_transcript_version_id']}) map={umap}",
        )

        # G2 既有合法证据 ID 不变、仍有效、仍指向既有版本
        ev = await conn.fetchrow(
            "SELECT is_valid, transcript_version_id FROM evidence_spans WHERE id = $1::uuid",
            ids["ev_real"],
        )
        record(
            "G2 既有合法证据 ID 不变且可解析",
            ev is not None and ev["is_valid"] is True and ev["transcript_version_id"] == ids["ver_real"],
            f"is_valid={ev['is_valid'] if ev else None} tx={ev['transcript_version_id'] if ev else None}",
        )

        # G2 版本不重建、不删除：版本 / 片段 / 证据 记录数保持不变
        tv = await conn.fetchval("SELECT count(*) FROM transcript_versions")
        seg = await conn.fetchval("SELECT count(*) FROM transcript_segments")
        evc = await conn.fetchval("SELECT count(*) FROM evidence_spans")
        record(
            "G2 版本/片段/证据不重建不删除",
            (tv, seg, evc) == (2, 2, 1),
            f"tv={tv} seg={seg} ev={evc}",
        )
    finally:
        await conn.close()


def main() -> None:
    db = _target_db()
    print(f"=== 第二轮补偿迁移验证（目标库 {db}）===")

    print("[1/4] 重建测试库 + vector 扩展")
    asyncio.run(_reset_and_create(db))

    print("[2/4] 迁移到 d5 并回退为「原版 d5」状态（严格约束 + 错误回填产物）")
    run_alembic("d5e6f7a8b9c0")
    ids = asyncio.run(_revert_to_old_d5_and_seed())

    print("[3/4] 迁移到 head（e6 + f7a8b9c0d1e2 第二轮补偿）")
    run_alembic("head")

    print("[4/4] 验证 G1 约束收敛 + G2 回填修复")
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
