"""迁移冒烟测试：临时库 → alembic upgrade head → 校验关键表 → 删除临时库。

用法：python scripts/_mig_smoke.py <db_name>
"""
import asyncio
import os
import subprocess
import sys

import asyncpg

BASE = "postgresql://postgres:postgres@localhost:5432"
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REQUIRED_TABLES = {
    "budget_accounts",
    "budget_reservations",
    "provider_permit_slots",
}


async def _run(cmd: list[str], db: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{db}"
    proc = subprocess.run(cmd, cwd=BACKEND, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print("STDOUT:\n", proc.stdout)
        print("STDERR:\n", proc.stderr)
        sys.exit(proc.returncode)


async def main(db: str):
    conn = await asyncpg.connect(BASE + "/postgres", timeout=5)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{db}"')
    finally:
        await conn.close()

    try:
        # pgvector 扩展须在 alembic 之前创建（initial schema 含 VECTOR 列）
        conn = await asyncpg.connect(BASE + "/" + db, timeout=5)
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        finally:
            await conn.close()

        await _run(
            [
                os.path.join(BACKEND, ".venv", "Scripts", "python.exe"),
                "-m",
                "alembic",
                "upgrade",
                "head",
            ],
            db,
        )

        conn = await asyncpg.connect(BASE + "/" + db, timeout=5)
        try:
            rows = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
            tables = {r[0] for r in rows}
            print("TABLES:", sorted(tables))
            missing = REQUIRED_TABLES - tables
            if missing:
                print("MISSING REQUIRED TABLES:", sorted(missing))
                sys.exit(2)
            # 校验 provider_permit_slots 固定槽位 0/1 已种子
            slots = await conn.fetch(
                "SELECT slot FROM provider_permit_slots ORDER BY slot"
            )
            print("PERMIT_SLOTS:", [r[0] for r in slots])
            # 校验 processing_jobs 新增列
            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='processing_jobs' AND column_name='control_wait_elapsed_seconds'"
            )
            print("COLUMN_OK:", bool(cols))
            if not cols:
                sys.exit(3)
        finally:
            await conn.close()
        print("MIGRATION SMOKE OK")
    finally:
        conn = await asyncpg.connect(BASE + "/postgres", timeout=5)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        finally:
            await conn.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
