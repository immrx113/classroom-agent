"""Harness 模块冒烟测试"""
import asyncio
from app.agents.harness import (
    BudgetGuard,
    BudgetExceededError,
    with_smart_retry,
    classify_error,
    CircuitBreaker,
    validate_agent_output,
    harness_wrap,
)
from app.agents.tools.meeting_ops import (
    search_knowledge,
    get_meeting_history,
    save_summary,
    send_notification,
)
from app.agents.tools.registry import TOOL_REGISTRY, list_tools


async def test_budget():
    print("\n=== 1. BudgetGuard ===")
    guard = BudgetGuard(run_id="test", max_tokens=10000, max_cost_usd=0.5)
    await guard.consume("summary_agent", tokens_in=1200, tokens_out=800, model="qwen-plus")
    print(f"  tokens={guard.used_tokens} cost=${guard.used_cost:.6f}")
    print(f"  node_usage={guard.node_usage}")
    # 测试超限
    try:
        big = BudgetGuard(run_id="t2", max_tokens=100, max_cost_usd=0.5)
        await big.consume("x", tokens_in=200, tokens_out=0, model="qwen-plus")
        print("  ERROR: 应当抛 BudgetExceededError")
    except BudgetExceededError as e:
        print(f"  OK 超限拦截: {e}")


async def test_breaker():
    print("\n=== 2. CircuitBreaker ===")
    cb = CircuitBreaker(name="test", fail_threshold=3, recovery_timeout=60)
    print(f"  initial: state={cb.state} allow={cb.allow()}")
    for _ in range(3):
        cb.record_failure("err")
    print(f"  after 3 fails: state={cb.state} allow={cb.allow()}")
    cb.record_success()
    print(f"  after success: state={cb.state} allow={cb.allow()}")


async def test_retry():
    print("\n=== 3. with_smart_retry ===")
    call_count = 0

    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("network unreachable")
        return "ok"

    result = await with_smart_retry(flaky, max_retries=3, base_delay=0.1, timeout=5)
    print(f"  result={result} attempts={call_count}")

    # 不可重试错误应立即抛出
    call_count2 = 0

    async def auth_fail():
        nonlocal call_count2
        call_count2 += 1
        raise PermissionError("authentication failed")

    try:
        await with_smart_retry(auth_fail, max_retries=3, base_delay=0.1, timeout=5)
        print("  ERROR: 应当抛出")
    except PermissionError:
        print(f"  OK 不可重试错误立即抛出，调用次数={call_count2}")


async def test_validator():
    print("\n=== 4. OutputValidator ===")
    # 合法行动项
    ok, msg, _ = await validate_agent_output(
        "action_items_agent",
        [{"title": "完成文档", "assignee": "张三", "due_date": "2026-08-01", "priority": "high"}],
    )
    print(f"  合法行动项: ok={ok} msg={msg}")
    # 非法 priority
    ok, msg, _ = await validate_agent_output(
        "action_items_agent",
        [{"title": "x", "assignee": None, "due_date": None, "priority": "urgent"}],
    )
    print(f"  非法priority: ok={ok} msg={msg[:50]}")
    # 空 title
    ok, msg, _ = await validate_agent_output(
        "action_items_agent",
        [{"title": "", "assignee": None, "due_date": None, "priority": "high"}],
    )
    print(f"  空title: ok={ok} msg={msg[:50]}")
    # 短摘要
    ok, msg, _ = await validate_agent_output("summary_agent", "太短")
    print(f"  短摘要: ok={ok} msg={msg[:50]}")


async def test_tool_registry():
    print("\n=== 5. ToolRegistry ===")
    tools = list_tools()
    print(f"  已注册 {len(tools)} 个工具:")
    for t in tools:
        confirm = " [需确认]" if t["requires_confirmation"] else ""
        print(f"    - {t['name']} ({t['risk']}){confirm}")

    # 调用未注册工具应被拒
    from app.agents.tools.registry import call_tool
    r = await call_tool("delete_database", agent_run_id="")
    print(f"  调用未注册工具: ok={r['ok']} error={r['error']}")


async def test_harness_wrap():
    print("\n=== 6. harness_wrap ===")

    @harness_wrap(node_name="test_node", timeout=5, validate_output=False)
    async def fake_node(state):
        return {"summary": "fake content", "errors": []}

    result = await fake_node({"agent_run_id": ""})
    print(f"  成功路径: summary={result.get('summary')[:20]}...")

    @harness_wrap(node_name="fail_node", timeout=2, validate_output=False)
    async def slow_node(state):
        import asyncio as _aio
        await _aio.sleep(5)
        return {}

    result = await slow_node({"agent_run_id": ""})
    print(f"  超时路径: errors={result.get('errors', [])}")


async def main():
    await test_budget()
    await test_breaker()
    await test_retry()
    await test_validator()
    await test_tool_registry()
    await test_harness_wrap()
    print("\n=== ALL PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
