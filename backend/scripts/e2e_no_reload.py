#!/usr/bin/env python3
"""端到端验证脚本（不触发后端 reload）

使用：
    cd backend
    PYTHONPATH=. .venv/bin/python scripts/e2e_no_reload.py 2>&1
"""

import asyncio
import uuid
from datetime import datetime, timezone

from app.db.session import async_session_factory
from app.models.meeting import Meeting
from app.models.transcript import Transcript
from app.services.summary_service import summary_service
from app.services.decision_graph_service import decision_graph_service


# 评审会议转写数据
TRANSCRIPTS = [
    ("张三", "各位，今天我们评审一下数据库选型方案。目前候选有 PostgreSQL 和 MySQL 两个选项。"),
    ("李四", "我提议用 PostgreSQL。我们的业务有大量 JSON 字段需要索引，PostgreSQL 的 JSONB 索引性能明显优于 MySQL。"),
    ("王五", "我倾向于 MySQL。MySQL 运维更成熟，我们 DBA 团队对 MySQL 更熟悉。"),
    ("李四", "但 PostgreSQL 在复杂查询和扩展性上更强，而且 pgvector 扩展可以支持向量检索，对我们后续做 AI 知识库很有帮助。"),
    ("张三", "综合考虑，我们定 PostgreSQL，下周开始接入。王五你安排 DBA 团队做 PostgreSQL 培训。"),
    ("张三", "另外，接口契约格式也定一下。REST 还是 GraphQL？"),
    ("李四", "我建议用 REST，团队更熟悉，工具链更成熟，上手方便。"),
    ("王五", "GraphQL 灵活性更好，前端可以按需查询，减少过度拉取。"),
    ("张三", "那我们定 REST，GraphQL 暂时不引入。等团队规模大了再考虑。"),
]


async def main():
    print("\n" + "=" * 70)
    print("  端到端验证：评审决策完整链路（不触发后端 reload）")
    print("=" * 70)

    # Step 1: 创建会议
    print("\n[Step 1] 创建评审会议...")
    async with async_session_factory() as db:
        meeting = Meeting(
            title="技术选型评审会 - 数据库与接口契约",
            description="评审数据库选型（PostgreSQL vs MySQL）及接口契约格式（REST vs GraphQL）",
            status="processed",
            start_time=datetime(2026, 7, 15, 14, 0, 0, tzinfo=timezone.utc),
            participants=["张三", "李四", "王五"],
        )
        db.add(meeting)
        await db.flush()
        meeting_id = meeting.id
        await db.commit()
        print(f"  ✅ 会议创建成功: id={meeting_id}")

    # Step 2: 注入转写数据
    print("\n[Step 2] 注入转写数据...")
    async with async_session_factory() as db:
        for i, (speaker, content) in enumerate(TRANSCRIPTS):
            t = Transcript(
                meeting_id=meeting_id,
                speaker=speaker,
                content=content,
                start_time=i * 10.0,
                end_time=(i + 1) * 10.0,
                seq_index=i,
            )
            db.add(t)
        await db.commit()
        print(f"  ✅ 注入 {len(TRANSCRIPTS)} 条转写记录")

    # Step 3: 触发纪要生成（含 decision_extractor）
    print("\n[Step 3] 触发 meeting_graph_v2 工作流...")
    print("  ⏳ 正在调用 LLM，请等待（约 30-60 秒）...")
    async with async_session_factory() as db:
        summary = await summary_service.generate_summary(db, meeting_id)
        if summary:
            print(f"  ✅ 纪要生成完成: status={summary.status}")
            if summary.content:
                print(f"     纪要预览: {summary.content[:150]}...")
        else:
            print("  ❌ 纪要生成失败")
            return

    # Step 4: 验证决策库
    print("\n[Step 4] 验证决策库数据...")
    async with async_session_factory() as db:
        from sqlalchemy import select, func
        from app.models.decision import DecisionOption, DecisionRelation

        # 查询决策数
        result = await db.execute(select(func.count(Decision.id)).where(Decision.meeting_id == meeting_id))
        decision_count = result.scalar_one()
        print(f"  ✅ 决策数量: {decision_count}")

        if decision_count > 0:
            # 查询决策详情
            result = await db.execute(select(Decision).where(Decision.meeting_id == meeting_id))
            decisions = list(result.scalars().all())

            for idx, decision in enumerate(decisions, 1):
                print(f"\n  决策 #{idx}:")
                print(f"    标题: {decision.title}")
                print(f"    决策时间: {decision.decided_at}")
                print(f"    决策人: {decision.decided_by}")
                print(f"    反对意见: {len(decision.objections) if decision.objections else 0} 条")
                for obj in (decision.objections or []):
                    print(f"      - {obj['from']}: {obj['content']}")
                print(f"    候选方案: {decision.chosen_option}")

                # 查询 options
                opts_result = await db.execute(
                    select(DecisionOption).where(DecisionOption.decision_id == decision.id)
                )
                options = list(opts_result.scalars().all())
                for opt in options:
                    print(f"      - {opt.name} {'✓ 已选' if opt.is_chosen else ''}")

        # 查询关系总数
        rel_count_result = await db.execute(select(func.count(DecisionRelation.id)))
        total_relations = rel_count_result.scalar_one()
        print(f"\n  ✅ 决策关系总数: {total_relations}")

    # Step 5: 验证 AI 对话 RAG 召回决策
    print("\n[Step 5] 验证 AI 对话 RAG 召回决策...")
    async with async_session_factory() as db:
        search_results = await decision_graph_service.search(db, "数据库选型", top_k=3)
        print(f"  ✅ 检索结果: {len(search_results)} 条")
        for r in search_results:
            print(f"    - {r['title']} (score: {r['score']:.2f})")

    # Step 6: 验证知识库检索（文档 RAG）
    print("\n[Step 6] 验证知识库 RAG 召回...")
    from app.services.knowledge_service import knowledge_service
    doc_results = await knowledge_service.search(db, "PostgreSQL JSONB", top_k=2)
    print(f"  ✅ 知识库检索: {len(doc_results)} 条")

    # Step 7: 验证双路 RRF 融合
    print("\n[Step 7] 验证 AI 对话双路 RAG 召回（文档 + 决策）...")
    from app.services.chat_service import chat_service
    fused = chat_service._rrf_fuse(doc_results, search_results, top_k=5)
    print(f"  ✅ RRF 融合: {len(fusededs)} 条")
    for i, r in enumerate(fused[:3], 1):
        source_type = r.get('source_type', '')
        print(f"    #{i}: {r['title']} ({source_type}) (RRF: {r.get('rrf_score', 0):.4f})")

    # Step 8: 查询关联决策验证双向关联
    print("\n[Step 8] 验证关联决策双向关联...")
    async with async_session_factory() as db:
        from app.models.decision import DecisionRelation
        # 查询决策 relations
        if decision_count > 0:
            rel_result = await db.execute(
                select(DecisionRelation, Decision)
                .join(Decision, DecisionRelation.target_decision_id == Decision.id)
                .where(DecisionRelation.source_decision_id == decisions[0].id)
            )
            relations = list(rel_result.all())
            print(f"  决策 #{decisions[0].title[:20]}...} 的关联决策: {len(relations)} 个")

    # Step 9: 验证反对意见前端展示
    print("\n[Step 9] 验证反对意见前端展示...")
    print("  请在前端访问决策详情页查看反对意见卡片")
    if decision_count > 0:
        print(f"  地址: http://localhost:5173/decisions/{decisions[0].id}")
    else:
        print(f"  地址: http://localhost:5173/decisions/")

    print("\n" + "=" * 70)
    print("  ✅ 端到端验证完成！")
    print("=" * 70)
    print(f"\n  访问前端验证:")
    print(f"    - 决策库列表: http://localhost:5173/decisions")
    if decision_count > 0:
        print(f"    - 决策详情页（含反对意见）: http://localhost:5173/decisions/{decisions[0].id}")
    print(f"    - AI 对话 RAG 召回: http://localhost:5173/chat  （问「数据库选型」应能看到决策 RAG）")
    print(f"\n  会议 ID: {meeting_id}")
    print(f"  (可通过会议详情页查看转写/纪要/决策)")


if __name__ == "__main__":
    asyncio.run(main())