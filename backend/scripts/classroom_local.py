"""Local trial commands. Never migrate or write yuan_meet."""
import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import settings
from sqlalchemy.engine import make_url

TARGET = "yuan_classroom_local"


def status():
    ledger = Path(settings.CLASSROOM_TRIAL_LEDGER)
    reserved = 0
    if ledger.exists():
        with closing(sqlite3.connect(ledger)) as conn:
            reserved = round(conn.execute("SELECT coalesce(sum(reserved_cny),0) FROM calls").fetchone()[0], 6)
    print(json.dumps({"database": make_url(settings.DATABASE_URL).database,
        "llm_model": settings.ORGANIZE_MODEL, "embedding_model": settings.INDEX_EMBEDDING_MODEL,
        "embedding_provider": settings.INDEX_EMBEDDING_PROVIDER,
        "api_key_configured": bool(settings.OPENAI_API_KEY),
        "oss_configured": all([settings.OSS_ACCESS_KEY_ID, settings.OSS_ACCESS_KEY_SECRET,
                               settings.OSS_ENDPOINT, settings.OSS_BUCKET_NAME]),
        "paid_calls_enabled": settings.CLASSROOM_PAID_CALLS_ENABLED,
        "reserved_cny_not_invoice": reserved, "trial_cap_cny": min(10, settings.CLASSROOM_TRIAL_BUDGET_CNY)},
        ensure_ascii=False, indent=2))


async def init_db():
    import asyncpg
    from dotenv import set_key
    source = make_url(settings.DATABASE_URL)
    url = source.set(drivername="postgresql", database="postgres")
    conn = await asyncpg.connect(url.render_as_string(hide_password=False))
    try:
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", TARGET):
            await conn.execute('CREATE DATABASE "yuan_classroom_local"')
    finally:
        await conn.close()
    target = source.set(database=TARGET)
    conn = await asyncpg.connect(target.set(drivername="postgresql").render_as_string(hide_password=False))
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()
    env = dict(os.environ, DATABASE_URL=target.render_as_string(hide_password=False), PYTHONPATH=str(ROOT))
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                            cwd=ROOT, env=env, capture_output=True)
    if result.returncode:
        raise RuntimeError("独立课堂库迁移失败，原.env和正式库未修改")
    set_key(str(ROOT / ".env"), "DATABASE_URL", target.render_as_string(hide_password=False))
    print("独立课堂库已就绪；.env已切换到yuan_classroom_local，原正式库未修改。请重启后端。")


async def trial():
    if make_url(settings.DATABASE_URL).database != TARGET:
        raise RuntimeError("先运行init，试用入口只允许写yuan_classroom_local")
    from app.services.oss_service import oss_service
    if not settings.CLASSROOM_PAID_CALLS_ENABLED or not oss_service.is_available:
        raise RuntimeError("OSS未就绪或真实调用未开启，未创建试用数据、未调用模型")
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.classroom_worker import main as run_worker
    from app.db.session import engine
    material = ROOT.parent / "docs/classroom-agent/trial-materials"
    state_path = ROOT / "runtime/classroom-trial-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    def save():
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    async def checked(response):
        if response.status_code >= 400:
            raise RuntimeError(f"本地API请求失败：HTTP {response.status_code}")
        return response.json()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local") as client:
            if "course_id" not in state:
                body = await checked(await client.post("/api/courses", json={"title": "本机试用·牛顿第二定律"}))
                state["course_id"] = body["id"]
                save()
            cid = state["course_id"]
            if "lecture_id" not in state:
                body = await checked(await client.post(f"/api/courses/{cid}/lectures", json={"title": "牛顿第二定律短课"}))
                state["lecture_id"] = body["id"]
                save()
            lid = state["lecture_id"]
            if "document_id" not in state:
                body = await checked(await client.post(f"/api/courses/{cid}/documents", files={
                    "file": ("newton-trial.md", (material / "newton-trial.md").read_bytes(), "text/markdown")}))
                state["document_id"] = body["id"]
                save()
            if "job_id" not in state:
                with (material / "newton-trial.wav").open("rb") as audio:
                    body = await checked(await client.post(f"/api/lectures/{lid}/audio", files={
                        "file": ("newton-trial.wav", audio, "audio/wav")}))
                state.update(body)
                save()
            # Uses exactly the same persistent service chain as the standalone worker.
            await run_worker(once=True)
            state["cycle"] = "completed"
            job = await checked(await client.get(f"/api/jobs/{state['job_id']}"))
            notes = await checked(await client.get(f"/api/lectures/{lid}/notes/current"))
            found = await checked(await client.post("/api/retrieval/search", json={
                "query": "牛顿第二定律", "course_id": cid, "lecture_id": lid, "top_k": 3}))
            state["checks"] = {"asr_succeeded": job["status"] == "succeeded",
                "note_published": bool(notes and notes["status"] == "published"),
                "retrieval_found": found["total"] > 0}
            state["note_id"] = notes["id"] if notes else None
            save()
            print(json.dumps({"checks": state["checks"], "note_id": state["note_id"],
                              "course_id": cid, "lecture_id": lid}, ensure_ascii=False))
            if not all(state["checks"].values()):
                raise RuntimeError("试用流程未全部成功；状态已保留，不会自动新建一套数据")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    os.chdir(ROOT)
    parser = argparse.ArgumentParser(description="当前电脑课堂试用")
    parser.add_argument("command", choices=["status", "init", "trial", "reconcile-budget"])
    args = parser.parse_args()
    if args.command == "status":
        status()
    elif args.command == "reconcile-budget":
        from app.services.trial_budget import reconcile_recorded_usage
        print(f"已按记录用量核算{reconcile_recorded_usage()}次请求，未删除历史账本")
        status()
    else:
        asyncio.run(init_db() if args.command == "init" else trial())
