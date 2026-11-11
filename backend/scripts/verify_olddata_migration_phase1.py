r"""阶段一·旧数据副本全链迁移验证（R4 / R5 复验）

从旧库状态（revision 293138585702，尚未引入课程领域表）出发，
塞入带「重复 seq_index / 负时间」的旧 meeting+transcripts，
再一路 ``alembic upgrade head`` 走到补偿迁移 d5e6f7a8b9c0，
验证：

- R4  全链迁移不再因重复 seq_index 抛 IntegrityError（回填前确定性去重）
- R4  负时间片段转毫秒时置空（不写非法负数）
- R5  mock → is_demo=True + 设活动指针；real → is_demo=False + 活动指针
- R5  未知来源（transcription_mode NULL）→ 隔离，不设活动指针
- R5  缺失音频 → storage_key NULL 显式状态
- R5  旧片段 ID 通过 source_transcript_id 保留映射
- 兼容期：所有旧会议归入「待整理历史资料」课程并标记 legacy_meeting

说明：旧库 ``meetings`` 原本没有 ``transcription_mode`` 列，该列由
e5f6a7b8c9d0 补齐（nullable）。因此先迁移到 e5f6a7b8c9d0 补列后，
再为样例补写 mock / real 模式，最后跑 head 的补偿回填——覆盖三种分类。

运行（在 backend 目录）：
  cd backend
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  $env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/yuan_meet_classroom_olddata_test"
  python scripts/verify_olddata_migration_phase1.py
"""

import asyncio
import sys
import uuid
from datetime import datetime, timezone

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
    # 维护库连接：仅用于 DROP/CREATE DATABASE
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


async def _seed_old_data() -> dict[str, uuid.UUID]:
    conn = await asyncpg.connect(_target_dsn())
    try:
        m1 = uuid.uuid4()  # mock：有音频 + 重复序号 + 负时间
        m2 = uuid.uuid4()  # unknown：无音频
        m3 = uuid.uuid4()  # real：有音频

        await conn.execute(
            "INSERT INTO meetings (id, title, status, audio_url) VALUES ($1::uuid, $2, $3, $4)",
            m1, "旧会议-演示转写", "completed", "mock://demo/lecture1.mp3",
        )
        await conn.execute(
            "INSERT INTO meetings (id, title, status, audio_url) VALUES ($1::uuid, $2, $3, $4)",
            m2, "旧会议-来源未知", "completed", None,
        )
        await conn.execute(
            "INSERT INTO meetings (id, title, status, audio_url) VALUES ($1::uuid, $2, $3, $4)",
            m3, "旧会议-真实转写", "completed", "oss://bucket/lecture3.mp3",
        )

        t1 = uuid.uuid4()  # m1 seq0 保留
        t2 = uuid.uuid4()  # m1 seq0 重复（去重丢弃）
        t3 = uuid.uuid4()  # m1 seq1 负时间
        t4 = uuid.uuid4()  # m2 seq0
        t5 = uuid.uuid4()  # m3 seq0
        t6 = uuid.uuid4()  # m2 seq1 倒置时间（start > end）

        await conn.execute(
            "INSERT INTO transcripts (id, meeting_id, speaker, content, start_time, end_time, seq_index, created_at) "
            "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::timestamptz)",
            t1, m1, "老师", "片段A", 0.0, 1.5, 0, datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        await conn.execute(
            "INSERT INTO transcripts (id, meeting_id, speaker, content, start_time, end_time, seq_index, created_at) "
            "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::timestamptz)",
            t2, m1, "老师", "片段A重复", 0.0, 1.5, 0, datetime(2020, 1, 2, tzinfo=timezone.utc),
        )
        await conn.execute(
            "INSERT INTO transcripts (id, meeting_id, speaker, content, start_time, end_time, seq_index, created_at) "
            "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::timestamptz)",
            t3, m1, "老师", "负时间片段", -1.0, 2.0, 1, datetime(2020, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        )
        await conn.execute(
            "INSERT INTO transcripts (id, meeting_id, speaker, content, start_time, end_time, seq_index, created_at) "
            "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::timestamptz)",
            t4, m2, "学生", "未知片段", 0.0, 3.0, 0, datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        await conn.execute(
            "INSERT INTO transcripts (id, meeting_id, speaker, content, start_time, end_time, seq_index, created_at) "
            "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::timestamptz)",
            t5, m3, "老师", "真实片段", 0.0, 5.0, 0, datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        await conn.execute(
            "INSERT INTO transcripts (id, meeting_id, speaker, content, start_time, end_time, seq_index, created_at) "
            "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::timestamptz)",
            t6, m2, "学生", "倒置时间片段", 5.0, 2.0, 1, datetime(2020, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        )

        return {"m1": m1, "m2": m2, "m3": m3, "t1": t1, "t3": t3, "t4": t4, "t5": t5, "t6": t6}
    finally:
        await conn.close()


async def _set_modes(ids: dict) -> None:
    conn = await asyncpg.connect(_target_dsn())
    try:
        await conn.execute(
            "UPDATE meetings SET transcription_mode = 'mock' WHERE id = $1::uuid", ids["m1"]
        )
        await conn.execute(
            "UPDATE meetings SET transcription_mode = 'real' WHERE id = $1::uuid", ids["m3"]
        )
        # m2 保持 NULL → 未知来源
    finally:
        await conn.close()


async def _verify(ids: dict) -> None:
    conn = await asyncpg.connect(_target_dsn())
    try:
        # 兼容期归属：历史课程 + legacy_meeting
        hcourse = await conn.fetchrow(
            "SELECT id FROM courses WHERE term = '历史导入' ORDER BY created_at LIMIT 1"
        )
        record("R5 历史课程已创建", hcourse is not None)
        if hcourse:
            rows = await conn.fetch(
                "SELECT legacy_meeting, course_id FROM meetings ORDER BY title"
            )
            record(
                "R5 旧会议全部归入历史课程并标记 legacy",
                all(r["legacy_meeting"] and r["course_id"] == hcourse["id"] for r in rows),
            )

        # M1 mock
        m1 = await conn.fetchrow(
            "SELECT active_audio_id, active_transcript_version_id FROM meetings WHERE id = $1::uuid",
            ids["m1"],
        )
        record("R5 mock 设活动指针", m1["active_audio_id"] is not None and m1["active_transcript_version_id"] is not None)
        if m1["active_audio_id"]:
            a1 = await conn.fetchrow(
                "SELECT storage_key, is_demo FROM audio_assets WHERE id = $1::uuid",
                m1["active_audio_id"],
            )
            record(
                "R5 mock is_demo=True + 保留音频 key",
                a1["is_demo"] is True and a1["storage_key"] == "mock://demo/lecture1.mp3",
            )
            v1 = await conn.fetchrow(
                "SELECT status FROM transcript_versions WHERE id = $1::uuid",
                m1["active_transcript_version_id"],
            )
            record("R5 mock 版本状态 active", v1["status"] == "active")
            segs = await conn.fetch(
                "SELECT seq_index, start_ms, end_ms, content, source_transcript_id "
                "FROM transcript_segments WHERE version_id = $1::uuid ORDER BY seq_index",
                m1["active_transcript_version_id"],
            )
            record("R4 重复序号去重后片段数=2", len(segs) == 2, f"got {len(segs)}")
            if len(segs) == 2:
                s0, s1 = segs[0], segs[1]
                record(
                    "R4 去重保留最早 + 秒转毫秒",
                    s0["content"] == "片段A" and s0["start_ms"] == 0 and s0["end_ms"] == 1500,
                )
                record(
                    "R4 负时间置空",
                    s1["content"] == "负时间片段" and s1["start_ms"] is None and s1["end_ms"] == 2000,
                )
                record(
                    "R5 旧片段 ID 映射保留",
                    s0["source_transcript_id"] == ids["t1"] and s1["source_transcript_id"] == ids["t3"],
                )

        # M2 unknown + 缺失音频
        m2 = await conn.fetchrow(
            "SELECT active_audio_id, active_transcript_version_id FROM meetings WHERE id = $1::uuid",
            ids["m2"],
        )
        record(
            "R5 未知来源隔离(不设活动指针)",
            m2["active_audio_id"] is None and m2["active_transcript_version_id"] is None,
        )
        v2 = await conn.fetchval(
            "SELECT id FROM transcript_versions WHERE lecture_id = $1::uuid", ids["m2"]
        )
        record("R5 未知来源版本状态 ready", v2 is not None)
        if v2:
            r2 = await conn.fetchrow(
                "SELECT status FROM transcript_versions WHERE id = $1::uuid", v2
            )
            record("R5 未知来源版本状态 ready", r2["status"] == "ready")
            a2 = await conn.fetchrow(
                "SELECT storage_key, is_demo FROM audio_assets WHERE lecture_id = $1::uuid",
                ids["m2"],
            )
            record(
                "R5 缺失音频 storage_key=NULL 显式状态",
                a2 is not None and a2["storage_key"] is None,
            )
            inv_segs = await conn.fetch(
                "SELECT seq_index, start_ms, end_ms, content FROM transcript_segments "
                "WHERE version_id = $1::uuid ORDER BY seq_index",
                v2,
            )
            inv1 = next((s for s in inv_segs if s["seq_index"] == 1), None)
            record(
                "F4 倒置时间成对清空",
                inv1 is not None and inv1["start_ms"] is None and inv1["end_ms"] is None,
                f"got {[(s['seq_index'], s['start_ms'], s['end_ms']) for s in inv_segs]}",
            )

        # M3 real
        m3 = await conn.fetchrow(
            "SELECT active_audio_id, active_transcript_version_id FROM meetings WHERE id = $1::uuid",
            ids["m3"],
        )
        record("R5 real 设活动指针", m3["active_audio_id"] is not None and m3["active_transcript_version_id"] is not None)
        if m3["active_audio_id"]:
            a3 = await conn.fetchrow(
                "SELECT storage_key, is_demo FROM audio_assets WHERE id = $1::uuid",
                m3["active_audio_id"],
            )
            record(
                "R5 real is_demo=False + 保留音频 key",
                a3["is_demo"] is False and a3["storage_key"] == "oss://bucket/lecture3.mp3",
            )
    finally:
        await conn.close()


def main() -> None:
    db = _target_db()
    print(f"=== 旧数据副本全链迁移验证（目标库 {db}）===")

    print("[1/5] 重建测试库 + vector 扩展")
    asyncio.run(_reset_and_create(db))

    print("[2/5] 迁移到旧库状态 293138585702")
    run_alembic("293138585702")

    print("[3/5] 塞入旧 meeting+transcripts（重复序号/负时间）")
    ids = asyncio.run(_seed_old_data())

    print("[4/5] 迁移到 e5f6a7b8c9d0（补齐列）→ 补写模式 → head（补偿回填）")
    run_alembic("e5f6a7b8c9d0")
    asyncio.run(_set_modes(ids))
    run_alembic("head")

    print("[5/5] 验证回填结果")
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
