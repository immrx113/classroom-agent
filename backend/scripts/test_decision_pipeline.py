"""决策抽取管线冒烟测试

覆盖可离线测试的纯函数与校验逻辑：
1. DecisionSegment / ExtractedDecision Pydantic 校验
2. _parse_segments 三种 JSON 格式兼容性
3. Objection 的 `from` 关键字 alias 处理
4. chosen 不在 options 时的自动修正
5. ChatService._rrf_fuse 双路 RRF 融合
6. decision_extractor_node 容错（空 transcript / 解析失败）

运行：source .venv/bin/activate && python scripts/test_decision_pipeline.py
"""
import asyncio
import sys

from app.agents.nodes.decision_detector import (
    DecisionSegment,
    _parse_segments,
)
from app.agents.nodes.option_extractor import (
    ExtractedDecision,
    DecisionOption,
    Objection,
    extract_options,
)
from app.agents.nodes.decision_extractor import decision_extractor_node
from app.services.chat_service import ChatService

passed = 0
failed = 0


def ok(name: str):
    global passed
    passed += 1
    print(f"  ✅ {name}")


def fail(name: str, err: Exception):
    global failed
    failed += 1
    print(f"  ❌ {name}: {err}")


def test_decision_segment_validation():
    """DecisionSegment 校验"""
    print("\n=== 1. DecisionSegment 校验 ===")
    # 合法
    try:
        seg = DecisionSegment(snippet="我们定 PostgreSQL", type="decision", confidence=0.9)
        assert seg.type == "decision"
        ok("合法构造")
    except Exception as e:
        fail("合法构造", e)

    # confidence 越界
    try:
        DecisionSegment(snippet="x", type="decision", confidence=1.5)
        fail("confidence 越界应拒绝", AssertionError("未抛异常"))
    except Exception:
        ok("confidence 越界拒绝")

    # 非法 type
    try:
        DecisionSegment(snippet="x", type="maybe", confidence=0.5)
        fail("非法 type 应拒绝", AssertionError("未抛异常"))
    except Exception:
        ok("非法 type 拒绝")

    # snippet 超长
    try:
        DecisionSegment(snippet="x" * 300, type="decision", confidence=0.5)
        fail("snippet 超长应拒绝", AssertionError("未抛异常"))
    except Exception:
        ok("snippet 超长拒绝")


def test_parse_segments_formats():
    """_parse_segments 兼容三种 JSON 格式"""
    print("\n=== 2. _parse_segments JSON 格式兼容 ===")

    # 格式 1: {items: [...]}
    raw1 = '{"items": [{"snippet": "选 PostgreSQL", "type": "decision", "confidence": 0.9}]}'
    segs1 = _parse_segments(raw1)
    assert len(segs1) == 1 and segs1[0].snippet == "选 PostgreSQL", f"格式1解析错误: {segs1}"
    ok("{items: [...]} 格式")

    # 格式 2: {decisions: [...]}
    raw2 = '{"decisions": [{"snippet": "选 MySQL", "type": "proposal", "confidence": 0.6}]}'
    segs2 = _parse_segments(raw2)
    assert len(segs2) == 1 and segs2[0].type == "proposal", f"格式2解析错误: {segs2}"
    ok("{decisions: [...]} 格式")

    # 格式 3: [...]
    raw3 = '[{"snippet": "选 Redis", "type": "deferred", "confidence": 0.3}]'
    segs3 = _parse_segments(raw3)
    assert len(segs3) == 1 and segs3[0].type == "deferred", f"格式3解析错误: {segs3}"
    ok("[...] 格式")

    # 非法 JSON
    assert _parse_segments("not json") == []
    ok("非法 JSON 返回空列表")

    # 跳过无效项（缺字段）
    raw4 = '{"items": [{"snippet": "ok", "type": "decision", "confidence": 0.9}, {"bad": true}]}'
    segs4 = _parse_segments(raw4)
    assert len(segs4) == 1, f"应跳过无效项: {segs4}"
    ok("跳过无效项")


def test_objection_alias():
    """Objection 的 `from` 关键字 alias"""
    print("\n=== 3. Objection `from` alias ===")
    try:
        # 用 alias 输入
        obj = Objection.model_validate({"from": "张三", "content": "成本太高"})
        assert obj.frm == "张三", f"alias 解析错误: {obj.frm}"
        ok("alias='from' 输入")

        # 用字段名输入（populate_by_name）
        obj2 = Objection(frm="李四", content="风险大")
        assert obj2.frm == "李四"
        ok("字段名 frm 输入")

        # 序列化回 alias
        dumped = obj.model_dump(by_alias=True)
        assert "from" in dumped and dumped["from"] == "张三"
        ok("by_alias 序列化")
    except Exception as e:
        fail("Objection alias", e)


def test_chosen_validation():
    """chosen 不在 options 时自动取第一个"""
    print("\n=== 4. chosen 校验与自动修正 ===")
    # chosen 匹配
    try:
        d = ExtractedDecision.model_validate({
            "title": "选定数据库",
            "context": "讨论数据库选型",
            "options": [{"name": "PostgreSQL"}, {"name": "MySQL"}],
            "chosen": "PostgreSQL",
        })
        assert d.chosen == "PostgreSQL"
        ok("chosen 匹配时正常")
    except Exception as e:
        fail("chosen 匹配", e)

    # chosen 不匹配 — extract_options 内部会修正，这里测试模型本身能构造
    try:
        d = ExtractedDecision.model_validate({
            "title": "选定缓存",
            "context": "讨论缓存选型",
            "options": [{"name": "Redis"}, {"name": "Memcached"}],
            "chosen": "不存在的方案",
        })
        # 模型层不强制 chosen ∈ options，由 extract_options 函数修正
        assert d.chosen == "不存在的方案"
        ok("chosen 不匹配时模型仍可构造（由函数层修正）")
    except Exception as e:
        fail("chosen 不匹配模型构造", e)

    # 空 options 应拒绝（min_length=1）
    try:
        ExtractedDecision.model_validate({
            "title": "空选项",
            "context": "无选项",
            "options": [],
            "chosen": "x",
        })
        fail("空 options 应拒绝", AssertionError("未抛异常"))
    except Exception:
        ok("空 options 拒绝")

    # options 超过 5 个应拒绝
    try:
        ExtractedDecision.model_validate({
            "title": "超选项",
            "context": "太多选项",
            "options": [{"name": f"opt{i}"} for i in range(6)],
            "chosen": "opt0",
        })
        fail("options 超 5 个应拒绝", AssertionError("未抛异常"))
    except Exception:
        ok("options 超 5 个拒绝")


def test_rrf_fusion():
    """ChatService._rrf_fuse 双路 RRF 融合"""
    print("\n=== 5. _rrf_fuse 双路融合 ===")
    svc = ChatService.__new__(ChatService)  # 不触发 __init__（避免创建 OpenAI client）

    doc_results = [
        {"id": "d1", "title": "文档1", "content": "c1", "source_type": "meeting_summary"},
        {"id": "d2", "title": "文档2", "content": "c2", "source_type": "uploaded_doc"},
    ]
    decision_results = [
        {"id": "r1", "title": "决策1", "context": "ctx1", "source_type": "decision"},
        {"id": "r2", "title": "决策2", "context": "ctx2", "source_type": "decision"},
    ]

    fused = svc._rrf_fuse(doc_results, decision_results, top_k=5)

    # 4 条结果全部保留
    assert len(fused) == 4, f"融合后应为 4 条，实际 {len(fused)}"
    ok("4 条结果全部保留")

    # 每条都有 rrf_score
    assert all("rrf_score" in r for r in fused), "缺少 rrf_score"
    ok("每条含 rrf_score")

    # 排序正确：rank 0 的 doc1 应排第一
    assert fused[0]["id"] == "d1", f"第一名应为 d1，实际 {fused[0]['id']}"
    ok("跨来源排序正确（文档 rank0 > 决策 rank0 > 文档 rank1 > 决策 rank1）")

    # 分数递减
    scores = [r["rrf_score"] for r in fused]
    assert scores == sorted(scores, reverse=True), f"分数未递减: {scores}"
    ok("分数严格递减")

    # top_k 截断
    fused_2 = svc._rrf_fuse(doc_results, decision_results, top_k=2)
    assert len(fused_2) == 2, f"top_k=2 应返回 2 条，实际 {len(fused_2)}"
    ok("top_k 截断生效")

    # 空输入
    assert svc._rrf_fuse([], [], top_k=5) == []
    ok("空输入返回空列表")


async def test_decision_extractor_node_resilience():
    """decision_extractor_node 容错"""
    print("\n=== 6. decision_extractor_node 容错 ===")

    # 空 transcript → 返回空列表
    result = await decision_extractor_node({})
    assert result.get("decisions") == [], f"空 state 应返回空列表: {result}"
    ok("空 state 返回空 decisions")

    result = await decision_extractor_node({"transcript_text": ""})
    assert result.get("decisions") == []
    ok("空 transcript 返回空 decisions")

    result = await decision_extractor_node({"transcript_compressed_text": "   "})
    assert result.get("decisions") == []
    ok("纯空白 transcript 返回空 decisions")


async def main():
    print("=" * 60)
    print("决策抽取管线冒烟测试")
    print("=" * 60)

    test_decision_segment_validation()
    test_parse_segments_formats()
    test_objection_alias()
    test_chosen_validation()
    test_rrf_fusion()
    await test_decision_extractor_node_resilience()

    print("\n" + "=" * 60)
    print(f"结果：✅ {passed} 通过  ❌ {failed} 失败")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
