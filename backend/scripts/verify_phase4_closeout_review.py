"""Independent review of existing Phase 04 frozen scenarios; no business changes."""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify_phase4_frozen as v


ORIGINAL_OUT = v.OUT
v.OUT = Path(os.environ.get("PHASE4_REVIEW_OUT", str(
    ORIGINAL_OUT.parent / "evidence-phase4-closeout-independent-20261029")))
v.OUT.mkdir(parents=True, exist_ok=True)
v._UPLOAD_TMP = v.OUT / "_uploads"
v._UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
v.settings.UPLOAD_DIR = str(v._UPLOAD_TMP)
original_tests = v.tests
original_search = v.search
observations = []


async def observed_search(client, query, **kwargs):
    response = await original_search(client, query, **kwargs)
    if query in {"P12V1-ANCHOR", "HIT-ANCHOR", "BLK-TWO-HIT"}:
        body = response.json()
        counts = [sum(map(len, r.get("content") or []))
                  + sum(map(len, r.get("adjacent") or []))
                  for r in body.get("results", [])]
        observations.append({"query": query, "index_status": body.get("index_status"),
                             "total": body.get("total"), "returned_char_counts": counts,
                             "returned_char_total": sum(counts),
                             "results": body.get("results", [])})
    return response


async def review_existing_observations():
    lag = next(o for o in observations if o["query"] == "P12V1-ANCHOR")
    marked_old = bool(lag["results"]) and all(
        r.get("is_current") is False for r in lag["results"])
    v.record("P04-12", "V2/I2 building 时旧索引检索显式标记（原固定场景）",
             lag["index_status"] in {"stale", "building", "updating"} or marked_old,
             {"index_status": lag["index_status"], "all_results_marked_old": marked_old,
              "result_is_current": [r.get("is_current") for r in lag["results"]]})
    for o in observations:
        if o["query"] in {"HIT-ANCHOR", "BLK-TWO-HIT"}:
            v.record("P04-10", "原固定查询实际返回的全部命中与邻接正文预算",
                     o["returned_char_total"] <= v.settings.INDEX_CONTEXT_BUDGET,
                     {k: o[k] for k in ("query", "total", "returned_char_counts",
                                        "returned_char_total")} | {
                         "budget": v.settings.INDEX_CONTEXT_BUDGET})


def reviewed_tests(current, client):
    return original_tests(current, client) + [
        ("P04-10/12", review_existing_observations())]


async def main():
    manifest = json.loads((ORIGINAL_OUT / "source-manifest.json").read_text(encoding="utf-8"))
    snapshot = {}
    for rel, expected in manifest.items():
        path = v.ROOT / rel
        actual = v.sha256(path) if path.exists() else None
        snapshot[rel] = {"matches_submission": actual == expected, "sha256": actual}
    (v.OUT / "submission-source-check.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    v.search = observed_search
    v.tests = reviewed_tests
    # Keep detailed evidence on disk; console only emits test status.
    def quiet_record(pid, check, ok, detail):
        row = dict(id=pid, check=check, result="通过" if ok else "不通过", detail=detail)
        v.RESULTS.append(row)
        print(f"{pid}: {row['result']} - {check}", flush=True)

    v.record = quiet_record
    code = 0
    try:
        await v.main()
    except SystemExit as e:
        code = int(e.code or 0)
    finally:
        await v.async_session_factory.kw["bind"].dispose()
        for name in v.DBS:
            assert name.startswith(v.PREFIX) and "test" in name
            await v.drop_db(name)
        conn = await v.asyncpg.connect(v.BASE + "/postgres", timeout=5)
        try:
            remaining = await conn.fetch("SELECT datname FROM pg_database WHERE datname = ANY($1::text[])", v.DBS)
        finally:
            await conn.close()
        uploads = v._UPLOAD_TMP.resolve()
        assert uploads.parent == v.OUT.resolve() and uploads.name == "_uploads"
        shutil.rmtree(uploads)
        (v.OUT / "cleanup.json").write_text(json.dumps({
            "databases": v.DBS, "remaining": [r["datname"] for r in remaining],
            "uploads_removed": not uploads.exists()}, indent=2), encoding="utf-8")
        # The submitted harness writes DSNs. Retain names only in this review.
        run_path = v.OUT / "run.json"
        if run_path.exists():
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["databases"] = v.DBS
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")
        (v.OUT / "observations.json").write_text(json.dumps(observations, ensure_ascii=False,
                                                            indent=2), encoding="utf-8")
    raise SystemExit(code)


if __name__ == "__main__":
    asyncio.run(main())
