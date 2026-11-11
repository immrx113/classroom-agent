#!/usr/bin/env python3
"""直接测试 DecisionDetector + OptionExtractor（绕过完整工作流）

使用：
    cd backend
    PYTHONPATH=. .venv/bin/python3 scripts/test_decision_extractor_direct.py
"""

import asyncio
import logging

from app.agents.nodes.decision_detector import detect_decisions
from app.agents.nodes.option_extractor import extract_options

logging.basicConfig(level=logging.DEBUG)

# 评审会议转写（确保有明确的「拍板」语言）
TRANSCRIPT_TEXT = """
张三：各位，今天我们评审一下数据库选型方案。目前候选有 PostgreSQL 和 MySQL 两个选项。
李四：我提议用 PostgreSQL。我们的业务有大量 JSON 字段需要索引，PostgreSQL 的 JSONB 索引性能明显优于 MySQL。
王五：我倾向于 MySQL。MySQL 运维更成熟，我们 DBA 团队对 MySQL 更熟悉。
张三：那我们定 PostgreSQL，下周开始接入。王五你安排 DBA 团队做 PostgreSQL 培训。
张三：另外，接口契约格式也定一下。REST 还是 GraphQL？
李四：我建议用 REST，团队更熟悉，工具链更成熟，上手方便。
王五：GraphQL 灵活性更好，前端可以按需查询，减少过度拉取。
张三：那我们定 REST，GraphQL 暂时不引入。等团队规模大了再考虑。
"""

async def test():
    print("\n=== 测试 DecisionDetector ===")
    segments = await detect_decisions(TRANSCRIPT_TEXT)
    print(f"检测到 {len(segments)} 个决策段")
    for i, seg in enumerate(segments, 1):
        print(f"\n  段{i}:")
        print(f"    类型: {seg.type}")
        print(f"    置信度: {seg.confidence}")
        print(f"    内容: {seg.snippet}")
    
    # 只对 type=decision 的段进行 OptionExtractor 测试
    decision_segments = [s for s in segments if s.type == "decision" and s.confidence >= 0.7]
    print(f"\n=== 测试 OptionExtractor（仅决策段）===")
    
    for i, seg in enumerate(decision_segments, 1):
        print(f"\n  测试段{i}:")
        extracted = await extract_options(seg, TRANSCRIPT_TEXT)
        if extracted:
            print(f"    标题: {extracted.title}")
            print(f"    已选方案: {extracted.chosen}")
            print(f"    反对意见: {len(extracted.objections)} 条")
            for obj in extracted.objections:
                print(f"      - {obj.frm}: {obj.content}")
        else:
            print(f"    ✗ 抽取失败")


if __name__ == "__main__":
    asyncio.run(test())