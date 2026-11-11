r"""阶段一·HTTP 层主体隔离回归（F1）

在独立测试库中直接塞入「其他主体」的课程/课时/决策/知识/会话，
再以固定本地主体（local-user）访问 HTTP 路由，验证：

- F1.1  会话：全局会话按 owner_id 隔离；他主会话详情 / 删除均 404，列表不可见
- F1.2  知识：他主知识列表不可见、删除 404、检索不召回
- F1.3  决策：他主决策列表不可见、详情 404；关联决策按范围过滤不外泄
- F1.4  召回：知识检索按范围隔离（fake model：无 embedding 时走全文召回）

说明：embedding 未配置 API key 时自动走全文召回，本测试不调用外部模型。

运行（在 backend 目录）：
  cd backend
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  python scripts/verify_http_scope_phase1.py
"""

import asyncio
import os
import sys
import uuid

TEST_DB = "yuan_meet_classroom_http_test"
os.environ.setdefault(
    "DATABASE_URL",
    f"postgresql+asyncpg://postgres:postgres@localhost:5432/{TEST_DB}",
)
# 强制无外部模型：清空 API key，使 embedding_service.embed_text 返回 None，
# 检索自动走全文召回（不调用 dashscope / OpenAI）。必须在 settings 导入前设置。
os.environ["OPENAI_API_KEY"] = ""

import asyncpg  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.config import settings  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
ZERO_VEC = [0.0] * 1024


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
    from app.db.session import async_session_factory
    from app.models.chat import ChatSession
    from app.models.course import Course
    from app.models.decision import Decision, DecisionRelation
    from app.models.knowledge_doc import KnowledgeDocument
    from app.models.meeting import Meeting

    async with async_session_factory() as db:
        fc = Course(owner_id="other-user", title="外校课程")
        db.add(fc)
        await db.flush()
        fl = Meeting(course_id=fc.id, title="外校课时", status="completed")
        db.add(fl)
        await db.flush()

        lc = Course(owner_id="local-user", title="本校课程")
        db.add(lc)
        await db.flush()
        ll = Meeting(course_id=lc.id, title="本校课时", status="completed")
        db.add(ll)
        await db.flush()

        d_local = Decision(meeting_id=ll.id, title="本地决策A", embedding=ZERO_VEC)
        d_foreign = Decision(meeting_id=fl.id, title="外校决策B", embedding=ZERO_VEC)
        db.add_all([d_local, d_foreign])
        await db.flush()
        db.add(
            DecisionRelation(
                source_decision_id=d_local.id,
                target_decision_id=d_foreign.id,
                relation_type="relates",
                similarity_score=0.9,
            )
        )

        k_foreign = KnowledgeDocument(
            title="外校知识", owner_id="other-user", source_type="uploaded_doc",
            content="zebra 外校机密内容", embedding=ZERO_VEC,
        )
        k_local = KnowledgeDocument(
            title="本校知识", owner_id="local-user", source_type="uploaded_doc",
            content="platypus 本校内容", embedding=ZERO_VEC,
        )
        db.add_all([k_foreign, k_local])

        s_foreign = ChatSession(owner_id="other-user", title="外校会话")
        s_local = ChatSession(owner_id="local-user", title="本校会话")
        db.add_all([s_foreign, s_local])
        await db.flush()

        await db.commit()
        return {
            "fl": str(fl.id), "ll": str(ll.id),
            "d_local": str(d_local.id), "d_foreign": str(d_foreign.id),
            "k_local": str(k_local.id), "k_foreign": str(k_foreign.id),
            "s_local": str(s_local.id), "s_foreign": str(s_foreign.id),
        }


async def _seed_and_verify() -> None:
    """在同一事件循环内完成塞数据与 HTTP 访问，避免共享 async 引擎的连接
    跨 asyncio.run 边界被复用（Windows 下会命中「Event loop is closed」）。"""
    ids = await _seed()
    await _verify(ids)
    # 显式释放应用引擎连接池，避免解释器退出时在已关闭的 loop 上终止连接
    from app.db.session import engine

    await engine.dispose()


async def _verify(ids: dict) -> None:
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # F1.1 会话
        r = await client.get("/api/chat/sessions")
        record("F1.1 会话列表状态 200", r.status_code == 200, f"got {r.status_code}")
        session_ids = {s["id"] for s in r.json()} if r.status_code == 200 else set()
        record(
            "F1.1 全局会话按 owner 隔离",
            ids["s_local"] in session_ids and ids["s_foreign"] not in session_ids,
            f"ids={sorted(session_ids)}",
        )
        r = await client.get(f"/api/chat/sessions/{ids['s_foreign']}/messages")
        record("F1.1 他主会话消息 404", r.status_code == 404, f"got {r.status_code}")
        r = await client.delete(f"/api/chat/sessions/{ids['s_foreign']}")
        record("F1.1 他主会话删除 404", r.status_code == 404, f"got {r.status_code}")

        # F1.2 知识
        r = await client.get("/api/knowledge/documents")
        record("F1.2 知识列表状态 200", r.status_code == 200, f"got {r.status_code}")
        doc_titles = {d["title"] for d in r.json()} if r.status_code == 200 else set()
        record(
            "F1.2 知识列表按 owner 隔离",
            "本校知识" in doc_titles and "外校知识" not in doc_titles,
            f"titles={sorted(doc_titles)}",
        )
        r = await client.delete(f"/api/knowledge/documents/{ids['k_foreign']}")
        record("F1.2 他主知识删除 404", r.status_code == 404, f"got {r.status_code}")
        r = await client.post("/api/knowledge/search", json={"query": "zebra", "top_k": 5})
        record("F1.4 他主知识检索不召回", r.status_code == 200 and r.json()["total"] == 0,
               f"got {r.status_code} total={r.json().get('total') if r.status_code == 200 else '?'}")
        r = await client.post("/api/knowledge/search", json={"query": "platypus", "top_k": 5})
        record("F1.4 本地知识检索可召回", r.status_code == 200 and r.json()["total"] >= 1,
               f"got {r.status_code} total={r.json().get('total') if r.status_code == 200 else '?'}")

        # F1.3 决策
        r = await client.get("/api/decisions")
        record("F1.3 决策列表状态 200", r.status_code == 200, f"got {r.status_code}")
        decision_titles = {d["title"] for d in r.json()["items"]} if r.status_code == 200 else set()
        record(
            "F1.3 决策列表按范围隔离",
            "本地决策A" in decision_titles and "外校决策B" not in decision_titles,
            f"titles={sorted(decision_titles)}",
        )
        r = await client.get(f"/api/decisions/{ids['d_foreign']}")
        record("F1.3 他主决策详情 404", r.status_code == 404, f"got {r.status_code}")
        r = await client.get(f"/api/decisions/{ids['d_local']}")
        ok = r.status_code == 200
        related = r.json().get("related_decisions", []) if ok else None
        record(
            "F1.3 关联决策按范围过滤不外泄",
            ok and related == [],
            f"got {r.status_code} related={related}",
        )


def main() -> None:
    db = _target_db()
    print(f"=== HTTP 层主体隔离回归（目标库 {db}）===")

    print("[1/4] 重建测试库 + vector 扩展")
    asyncio.run(_reset_and_create(db))

    print("[2/4] alembic upgrade head")
    run_alembic("head")

    print("[3/4] 塞入他主 + 本地数据")
    print("[4/4] 以本地主体访问 HTTP 路由")
    asyncio.run(_seed_and_verify())

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
