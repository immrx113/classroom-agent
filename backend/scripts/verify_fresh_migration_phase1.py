r"""阶段一·空库全链迁移验证（路径 1）

从空库出发 ``alembic upgrade head``，验证：

- 迁移可一路走到当前迁移目录的 head（由 ScriptDirectory 动态读取），无运行时错误
- 空库回填幂等：创建「待整理历史资料」课程，但不产生任何会议/版本
- 归属列（courses / chat_sessions / knowledge_documents.owner_id）存在且 NOT NULL
- 转写片段 source_transcript_id 映射列存在
- 全部约束（片段时间 / 证据定位 / 单来源 / 产物白名单）就位

运行（在 backend 目录）：
  cd backend
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  $env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/yuan_meet_classroom_fresh_test"
  python scripts/verify_fresh_migration_phase1.py
"""

import asyncio
import sys

import asyncpg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

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


def expected_heads() -> set[str]:
    """从当前 Alembic 迁移目录动态读取预期 head（不硬编码具体版本）。"""
    cfg = Config("alembic.ini")
    return set(ScriptDirectory.from_config(cfg).get_heads())


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


async def _verify() -> None:
    conn = await asyncpg.connect(_target_dsn())
    try:
        rows = await conn.fetch("SELECT version_num FROM alembic_version")
        actual = {r["version_num"] for r in rows}
        expected = expected_heads()
        record(
            "迁移到达 head",
            actual == expected,
            f"expected={sorted(expected)} got={sorted(actual)}",
        )

        # 领域表就位
        tables = {
            "courses", "meetings", "audio_assets", "transcript_versions",
            "transcript_segments", "note_versions", "source_documents",
            "source_document_versions", "evidence_spans", "artifact_evidence",
            "chat_sessions", "knowledge_documents", "decisions", "summaries",
        }
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        present = {r["table_name"] for r in rows}
        missing = tables - present
        record("领域表全部就位", not missing, f"missing={sorted(missing)}")

        # 归属列 NOT NULL
        for tbl in ("courses", "chat_sessions", "knowledge_documents"):
            col = await conn.fetchrow(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'owner_id'",
                tbl,
            )
            record(f"{tbl}.owner_id NOT NULL", col is not None and col["is_nullable"] == "NO",
                   f"got {col['is_nullable'] if col else None}")

        # 片段映射列
        src = await conn.fetchrow(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'transcript_segments' AND column_name = 'source_transcript_id'"
        )
        record("transcript_segments.source_transcript_id 存在", src is not None,
               f"got {src['is_nullable'] if src else None}")

        # 空库回填：历史课程创建，但不产生会议
        hcourse = await conn.fetchval(
            "SELECT count(*) FROM courses WHERE term = '历史导入'"
        )
        mcount = await conn.fetchval("SELECT count(*) FROM meetings")
        record("空库回填创建历史课程 + 零会议", hcourse == 1 and mcount == 0,
               f"hcourse={hcourse} meetings={mcount}")

        # 约束就位
        constraints = {
            "ck_evidence_span_single_source", "ck_evidence_span_transcript_span",
            "ck_evidence_span_document_block", "ck_evidence_span_start_nonneg",
            "ck_evidence_span_end_nonneg", "ck_evidence_span_time_order",
            "ck_transcript_segment_start_nonneg", "ck_transcript_segment_end_nonneg",
            "ck_transcript_segment_time_order", "ck_artifact_evidence_type",
        }
        con_rows = await conn.fetch("SELECT conname FROM pg_constraint")
        con_present = {r["conname"] for r in con_rows}
        con_missing = constraints - con_present
        record("全部约束就位", not con_missing, f"missing={sorted(con_missing)}")
    finally:
        await conn.close()


def main() -> None:
    db = _target_db()
    print(f"=== 空库全链迁移验证（目标库 {db}）===")

    print("[1/3] 重建测试库 + vector 扩展")
    asyncio.run(_reset_and_create(db))

    print("[2/3] alembic upgrade head")
    run_alembic("head")

    print("[3/3] 验证空库结构 + 空库回填")
    asyncio.run(_verify())

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
