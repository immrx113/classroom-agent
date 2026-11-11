"""Explicit short live probe. No DB/OSS writes; consumes two bounded API calls."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.services.organize_model import RealOrganizeModel, ChunkInput
from app.services.embedding_service import ClassroomEmbedding


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="确认发起真实收费调用")
    args = parser.parse_args()
    if not args.live:
        parser.error("仅在明确批准真实费用后使用--live")
    settings.CLASSROOM_PAID_CALLS_ENABLED = True
    outdir = Path(__file__).resolve().parents[2] / "docs/classroom-agent/acceptance/evidence-classroom-real-integration-20261029"
    outdir.mkdir(parents=True, exist_ok=True)
    results = []
    core = [
        {"seq_index": 0, "content": "牛顿第二定律指出，合外力等于质量乘以加速度，公式是F等于ma。"},
        {"seq_index": 1, "content": "今天的作业是计算质量两千克、加速度三米每二次方秒的物体所受合外力，下次课前提交。"},
    ]
    try:
        organized = await RealOrganizeModel().extract_chunk(ChunkInput(0, core, []))
        known = {s["seq_index"]: s["content"] for s in core}
        citations = [c for k in ("conclusions", "tasks") for c in organized[k]]
        valid = bool(organized["conclusions"]) and all(
            c.get("segment_index") in known and isinstance(c.get("quote"), str)
            and bool(c["quote"].strip()) and c["quote"] in known[c["segment_index"]]
            for c in citations)
        results.append({"check": "qwen-plus真实整理与原文引用", "passed": valid,
                        "conclusions": len(organized["conclusions"]), "tasks": len(organized["tasks"])})
    except Exception as exc:
        results.append({"check": "qwen-plus真实整理", "passed": False,
                        "error_type": type(exc).__name__, "http_status": getattr(exc, "status_code", None)})
    try:
        vectors = await ClassroomEmbedding().embed_batch([
            "牛顿第二定律：合外力等于质量乘以加速度。", "质量与加速度怎样决定合外力？"])
        results.append({"check": "text-embedding-v3真实向量", "passed": len(vectors) == 2 and all(len(v) == 1024 for v in vectors),
                        "count": len(vectors), "dimensions": [len(v) for v in vectors]})
    except Exception as exc:
        results.append({"check": "text-embedding-v3真实向量", "passed": False,
                        "error_type": type(exc).__name__, "http_status": getattr(exc, "status_code", None)})
    (outdir / "live-provider-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))
    raise SystemExit(0 if all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
