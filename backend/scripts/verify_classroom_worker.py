"""One-cycle upload -> ASR -> real adapter contract -> notes -> index, isolated DB."""
import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_phase4_frozen as v
from app.classroom_worker import run_cycle
from app.services.asr_provider import FakeASRProvider
from app.services.organize_model import FakeOrganizeModel

v.OUT = v.OUT.parent / "evidence-classroom-real-integration-20261029/worker"
v.OUT.mkdir(parents=True, exist_ok=True)
v._UPLOAD_TMP = v.OUT / "_uploads"
v._UPLOAD_TMP.mkdir(exist_ok=True)
v.settings.UPLOAD_DIR = str(v._UPLOAD_TMP)


async def check(current, client):
    current["subject_id"] = "worker-review"
    v.settings.LOCAL_SUBJECT_ID = "worker-review"
    lecture, course = await v.create_lecture("worker-review", "worker-short-course")
    path = v.ROOT.parent / "docs/classroom-agent/trial-materials/newton-trial.wav"
    uploaded = await client.post(f"/api/lectures/{lecture}/audio", files={
        "file": ("newton.wav", path.read_bytes(), "audio/wav")})
    assert uploaded.status_code == 201, uploaded.status_code
    provider = FakeASRProvider()
    provider.segments = [
        {"speaker": "教师", "content": "牛顿第二定律：合外力等于质量乘加速度。", "start_ms": 0, "end_ms": 2000},
        {"speaker": "教师", "content": "【作业】写出公式，下次课前提交。", "start_ms": 2000, "end_ms": 4000},
    ]
    embed = v.DeterministicEmbedding(1024)
    def interrupt():
        raise v.index_module.BuildInterrupted()
    v.index_module._build_hook = interrupt
    try:
        await run_cycle(provider=provider, model=FakeOrganizeModel(), embed=embed)
    except v.index_module.BuildInterrupted:
        pass
    finally:
        v.index_module._build_hook = None
    stats = await run_cycle(provider=provider, model=FakeOrganizeModel(), embed=embed)
    note = (await client.get(f"/api/lectures/{lecture}/notes/current")).json()
    found = (await v.search(client, "牛顿第二定律", course_id=course)).json()
    job = (await client.get(f"/api/jobs/{uploaded.json()['job_id']}")).json()
    current_audio = await client.get(f"/api/meetings/{lecture}/audio")
    async with v.async_session_factory() as db:
        versions = (await db.execute(v.select(v.IndexVersion).where(
            v.IndexVersion.course_id == course))).scalars().all()
    first = {"asr": job["status"] == "succeeded",
             "note": note is not None and note["status"] == "published",
             "index": found["total"] >= 1,
             "provider_submissions": provider.submit_calls,
             "current_audio_readable": current_audio.status_code == 200 and current_audio.content[:4] == b"RIFF",
             "resumed_same_index": len(versions) == 1}
    before = [(r["source_type"], r["source_version_id"]) for r in found["results"]]
    await run_cycle(provider=provider, model=FakeOrganizeModel(), embed=embed)
    found_again = (await v.search(client, "牛顿第二定律", course_id=course)).json()
    same = before == [(r["source_type"], r["source_version_id"]) for r in found_again["results"]]
    v.record("LOCAL-WORKER", "上传到笔记/索引一轮完成，二轮不重复提交", all([
        first["asr"], first["note"], first["index"], first["current_audio_readable"], first["resumed_same_index"], provider.submit_calls == 1, same]),
        first | {"second_cycle_same_sources": same})


async def main():
    v.tests = lambda current, client: [("LOCAL-WORKER", check(current, client))]
    try:
        await v.main()
    finally:
        await v.async_session_factory.kw["bind"].dispose()
        for name in v.DBS:
            assert name.startswith(v.PREFIX)
            await v.drop_db(name)
        uploads = v._UPLOAD_TMP.resolve()
        assert uploads.parent == v.OUT.resolve()
        shutil.rmtree(uploads)
        run_path = v.OUT / "run.json"
        if run_path.exists():
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["databases"] = v.DBS
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
