"""Single-computer trial cap. Reserve ¥1 before each bounded provider request.

Reservations are persistent; unknown/failed requests retain the full reservation.
Known usage settles to a conservative cost ceiling, NOT the provider's bill.
Only the short trial's
qwen-plus / text-embedding-v3 / <=120s Paraformer requests use this ledger.
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.config import settings


def reserve_call(kind: str, model: str) -> str:
    if not settings.CLASSROOM_PAID_CALLS_ENABLED:
        raise RuntimeError("课堂真实调用尚未开启：请完成OSS配置后显式开启CLASSROOM_PAID_CALLS_ENABLED")
    if (kind, model) not in {
        ("organize", "qwen-plus"), ("embedding", "text-embedding-v3"),
        ("asr", "paraformer-v2"),
    }:
        raise RuntimeError("试用预算仅核准qwen-plus/text-embedding-v3/paraformer-v2")
    path = Path(settings.CLASSROOM_TRIAL_LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    import uuid
    request_id = uuid.uuid4().hex
    with closing(sqlite3.connect(path, timeout=10)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, kind TEXT, model TEXT, reserved_cny INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP, usage TEXT)")
        conn.execute("BEGIN IMMEDIATE")
        used = conn.execute("SELECT coalesce(sum(reserved_cny),0) FROM calls").fetchone()[0]
        if used + 1 > min(settings.CLASSROOM_TRIAL_BUDGET_CNY, 10):
            raise RuntimeError("本轮10元试用预算预留额度已用尽，已停止新的收费请求")
        conn.execute("INSERT INTO calls(id,kind,model,reserved_cny) VALUES (?,?,?,1)",
                     (request_id, kind, model))
    return request_id


def record_usage(request_id: str, usage: dict) -> None:
    # No source text, credentials or signed URLs in the ledger.
    with closing(sqlite3.connect(settings.CLASSROOM_TRIAL_LEDGER, timeout=10)) as conn, conn:
        row = conn.execute("SELECT kind FROM calls WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("预算记录不存在")
        ceiling = _usage_ceiling(row[0], usage)
        conn.execute("UPDATE calls SET usage=?, reserved_cny=? WHERE id=?",
                     (json.dumps(usage), ceiling, request_id))


def _usage_ceiling(kind: str, usage: dict) -> float:
    # Deliberately above the approved short-request models' published rates.
    # Missing or malformed usage keeps the entire ¥1 allocation; never assume zero.
    if kind == "asr":
        duration = usage.get("duration_ms")
        if isinstance(duration, (int, float)) and 0 < duration <= 120000:
            return round(max(0.001, ((int(duration) + 999) // 1000) * 0.001), 6)
    else:
        tokens = usage.get("total_tokens")
        if isinstance(tokens, int) and tokens > 0:
            rate = 100 if kind == "organize" else 10
            return round(min(1, max(0.001, tokens * rate / 1_000_000)), 6)
    return 1


def reconcile_recorded_usage() -> int:
    """Re-account old known usage without deleting/resetting any request record."""
    with closing(sqlite3.connect(settings.CLASSROOM_TRIAL_LEDGER, timeout=10)) as conn:
        rows = conn.execute("SELECT id, usage FROM calls WHERE usage IS NOT NULL").fetchall()
    for request_id, usage in rows:
        record_usage(request_id, json.loads(usage))
    return len(rows)
