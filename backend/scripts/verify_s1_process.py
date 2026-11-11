r"""S1 独立进程证据驱动（CA-03/04/05/06/11 跨进程演练）。

针对 docs/classroom-agent/acceptance/evidence-checkpoint-ai-control-20261030/S1-fix-review-round3/closeout-review.md
第 E 节的 5 项「证据不足」，以真实独立 worker 进程（scripts/s1_process_worker.py）重演：

- CA-03：进程 1 完成 K01~K18 持久提交后退出；进程 2 恢复同一 job，持久调用审计只多出 K19/K20，20/20 覆盖。
- CA-04：进程 1 汇总结果与 Checkpoint 提交后退出；进程 2 恢复，summary 总调用 1 次并完成笔记发布。
- CA-05：同一 job 发布事务 commit 前退出（无半发布）→ 恢复（1 份有效笔记/Intent）→ 重投（无重复产物、无重复 summary）。
- CA-06：task_id 已存 / asr.result 已存两个故障点分别由新进程恢复；不重 submit/poll；版本/片段/Checkpoint 原子提交 + 重投幂等。
- CA-11：两进程真实竞争领取；60s 租约 / 10s 心跳健康长调用 >60s 不误接管；失联到期接管 epoch 递增；旧 epoch 续租/改写均被拒。

自动验收不调用真实收费接口：全程 fake ASR / FakeOrganizeModel / DeterministicEmbedding 替身，
数据库为随机隔离测试库（verify_checkpoint_ai_control 导入即建立），绝不触碰生产库 yuan_meet。

运行（backend 目录）：
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  .\.venv\Scripts\python.exe scripts/verify_s1_process.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import verify_checkpoint_ai_control as v  # noqa: E402  导入即建立随机隔离库

from app.models.meeting import Meeting  # noqa: E402
from app.models.note_version import NoteVersion  # noqa: E402
from app.models.organize import ChunkItem, ChunkManifest, IndexIntent  # noqa: E402
from app.models.processing_job import (  # noqa: E402
    JobCheckpoint,
    ProcessingJob,
    ProviderCall,
)
from app.models.transcript_version import (  # noqa: E402
    TranscriptSegment,
    TranscriptVersion,
)
from sqlalchemy import func, select  # noqa: E402

ROOT = v.ROOT
WORKER = ROOT / "scripts" / "s1_process_worker.py"
DRIVER = ROOT / "scripts" / "verify_s1_process.py"
PY = sys.executable
OUT = v.OUT_DEFAULT.parent / "S1-fix-review-round3" / "process-evidence"

RESULTS: list[dict] = []
STATE: dict = {}


def record(pid: str, check: str, ok: bool, detail) -> None:
    row = dict(id=pid, check=check, result="通过" if ok else "不通过", detail=detail)
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


def _env() -> dict:
    e = dict(os.environ)
    e["DATABASE_URL"] = v.asyncpg_dsn(v.MAIN_DB)
    e["PYTHONPATH"] = "."
    e["PYTHONUTF8"] = "1"
    e["INDEX_EMBEDDING_PROVIDER"] = "deterministic"
    e["CLASSROOM_PAID_CALLS_ENABLED"] = "false"
    e.setdefault("OPENAI_API_KEY", "")
    e.setdefault("DASHSCOPE_API_KEY", "")
    return e


def _cmd(role: str, job_id, meta_path, **kw) -> list[str]:
    cmd = [PY, str(WORKER), "--role", role, "--job-id", str(job_id), "--meta", str(meta_path)]
    for k, val in kw.items():
        cmd += [f"--{k.replace('_', '-')}", str(val)]
    return cmd


def run_worker(role: str, job_id, meta_path, timeout: int = 180, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        _cmd(role, job_id, meta_path, **kw), env=_env(), cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def spawn_worker(role: str, job_id, meta_path, **kw) -> subprocess.Popen:
    return subprocess.Popen(
        _cmd(role, job_id, meta_path, **kw), env=_env(), cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )


def read_meta(p) -> dict:
    p = Path(p)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"read_error": repr(e)}


# ── 持久审计查询助手 ──────────────────────────────────────────────────
async def n_calls(db, prefix: str) -> int:
    """provider_calls 中以 prefix 开头的 operation_key 条数（前缀不含 %/_ 通配符）。"""
    return (
        await db.execute(
            select(func.count()).select_from(ProviderCall)
            .where(ProviderCall.operation_key.like(prefix + "%"))
        )
    ).scalar_one()


async def extract_chunk_keys(db, jid) -> list[int]:
    rows = (
        await db.execute(
            select(ProviderCall.operation_key).where(
                ProviderCall.operation_key.like(f"organize.extract:{jid}:%")
            )
        )
    ).scalars().all()
    return sorted(int(k.rsplit(":", 1)[1]) for k in rows)


async def publish_snapshot(db, jid) -> dict:
    """note / active / IndexIntent / note.publish Checkpoint 快照。"""
    j = await db.get(ProcessingJob, jid)
    note = await db.get(NoteVersion, j.output_version) if j and j.output_version else None
    meeting = await db.get(Meeting, j.resource_id) if j else None
    intent_count = 0
    note_count = 0
    pub_cp = None
    if j:
        note_count = (
            await db.execute(
                select(func.count()).select_from(NoteVersion)
                .where(NoteVersion.lecture_id == j.resource_id)
            )
        ).scalar_one()
        if j.output_version:
            intent_count = (
                await db.execute(
                    select(func.count()).select_from(IndexIntent)
                    .where(IndexIntent.note_version_id == j.output_version)
                )
            ).scalar_one()
        pub_cp = (
            await db.execute(
                select(JobCheckpoint).where(
                    JobCheckpoint.job_id == j.id,
                    JobCheckpoint.step_key == "note.publish",
                )
            )
        ).scalars().first()
    return dict(
        job_status=j.status if j else None,
        output_version=str(j.output_version) if j and j.output_version else None,
        note_status=note.status if note else None,
        note_count=note_count,
        active_note=str(meeting.active_note_version_id) if meeting and meeting.active_note_version_id else None,
        active_transcript=str(meeting.active_transcript_version_id) if meeting and meeting.active_transcript_version_id else None,
        intent_count=intent_count,
        note_publish_cp=pub_cp.status if pub_cp else None,
    )


# ── CA-03：分块恢复 20/20 ─────────────────────────────────────────────
async def ca03() -> None:
    out_dir = OUT / "ca03"
    out_dir.mkdir(parents=True, exist_ok=True)
    mid, _ = await v.create_lecture(v.SUBJECT_A, "ca03-L1")
    _, vid = await v.add_transcript(mid, v.make_long_segments(n=20, base=2000))
    jid = await v.make_organize_job(mid, vid)

    m1 = out_dir / "worker-1.json"
    r1 = run_worker("organize-crash18", jid, m1, crash_chunk=18)
    meta1 = read_meta(m1)
    async with v.async_session_factory() as db:
        n1 = await n_calls(db, f"organize.extract:{jid}:")
        keys1 = await extract_chunk_keys(db, jid)

    await v.expire_lease(jid)
    m2 = out_dir / "worker-2.json"
    r2 = run_worker("organize-finish", jid, m2)
    meta2 = read_meta(m2)
    async with v.async_session_factory() as db:
        n2 = await n_calls(db, f"organize.extract:{jid}:")
        n_sum = await n_calls(db, f"organize.summary:{jid}")
        keys2 = await extract_chunk_keys(db, jid)
        j = await db.get(ProcessingJob, jid)
        note = await db.get(NoteVersion, j.output_version) if j.output_version else None
        # 每块恰好 1 次外发：崩溃的 K18 不记为外发，恢复只补 K19/K20
        per_chunk_rows = (
            await db.execute(
                select(ProviderCall.operation_key, func.count(ProviderCall.id))
                .where(ProviderCall.operation_key.like(f"organize.extract:{jid}:%"))
                .group_by(ProviderCall.operation_key)
            )
        ).all()
        per_chunk = {k: c for k, c in per_chunk_rows}

    delta = n2 - n1
    ok = (
        meta1.get("crashed") is True
        and meta1.get("failed") is not True
        and n1 == 18 and sorted(keys1) == list(range(18))
        and n2 == 20 and n_sum == 1 and delta == 2
        and sorted(keys2) == list(range(20))
        and all(c == 1 for c in per_chunk.values())
        and j.status == "succeeded" and note is not None and note.status == "published"
    )
    record(
        "CA-03",
        "进程1完成K01~K18退出；进程2恢复只补K19/K20，20/20覆盖，summary=1",
        ok,
        dict(
            p1_crashed=meta1.get("crashed"), p1_returncode=r1.returncode,
            p2_returncode=r2.returncode, extract_before=n1, extract_after=n2,
            extract_delta=delta, summary_calls=n_sum, chunk_keys=keys2,
            per_chunk={k: c for k, c in per_chunk.items()},
            job_status=j.status, note_status=note.status if note else None,
        ),
    )
    STATE["ca03"] = dict(
        job_id=str(jid), extract_before=n1, extract_after=n2, summary_calls=n_sum,
        per_chunk={k: c for k, c in per_chunk.items()},
    )


# ── CA-04：最终汇总恢复 ───────────────────────────────────────────────
async def ca04() -> None:
    out_dir = OUT / "ca04"
    out_dir.mkdir(parents=True, exist_ok=True)
    mid, _ = await v.create_lecture(v.SUBJECT_A, "ca04-L1")
    _, vid = await v.add_transcript(mid, v.make_long_segments(n=2, base=200))
    jid = await v.make_organize_job(mid, vid)

    m1 = out_dir / "worker-1.json"
    run_worker("organize-summary-crash", jid, m1)
    meta1 = read_meta(m1)
    async with v.async_session_factory() as db:
        sum_cp = await v.get_checkpoint(db, jid, "organize.summary")
        n_sum_1 = await n_calls(db, f"organize.summary:{jid}")
        j1 = await db.get(ProcessingJob, jid)

    await v.expire_lease(jid)
    m2 = out_dir / "worker-2.json"
    run_worker("organize-finish", jid, m2)
    meta2 = read_meta(m2)
    async with v.async_session_factory() as db:
        n_sum_2 = await n_calls(db, f"organize.summary:{jid}")
        j2 = await db.get(ProcessingJob, jid)
        note = await db.get(NoteVersion, j2.output_version) if j2.output_version else None

    ok = (
        meta1.get("crashed") is True
        and sum_cp is not None and sum_cp.status == "succeeded"
        and n_sum_1 == 1
        and j1.output_version is None  # summary 已提交但笔记尚未发布
        and n_sum_2 == 1  # 恢复复用，不重调 summary
        and j2.status == "succeeded" and note is not None and note.status == "published"
    )
    record(
        "CA-04",
        "summary与Checkpoint提交后退出；新进程恢复，summary总调用1次并发布有效笔记",
        ok,
        dict(
            p1_crashed=meta1.get("crashed"), summary_cp=sum_cp.status if sum_cp else None,
            summary_calls_before=n_sum_1, summary_calls_after=n_sum_2,
            output_version_before=str(j1.output_version) if j1.output_version else None,
            job_status=j2.status, note_status=note.status if note else None,
        ),
    )
    STATE["ca04"] = dict(
        job_id=str(jid), summary_calls=n_sum_2,
        output_version=str(j2.output_version) if j2.output_version else None,
    )


# ── CA-05：发布原子性与重投（同一 job）───────────────────────────────
async def ca05() -> None:
    out_dir = OUT / "ca05"
    out_dir.mkdir(parents=True, exist_ok=True)
    mid, _ = await v.create_lecture(v.SUBJECT_A, "ca05-L1")
    _, vid = await v.add_transcript(mid, v.make_long_segments(n=2, base=200))
    jid = await v.make_organize_job(mid, vid)

    # 1) commit 前中断：发布事务 commit 前进程退出
    m1 = out_dir / "worker-1.json"
    r1 = run_worker("organize-publish-kill", jid, m1, timeout=120)
    async with v.async_session_factory() as db:
        snap_a = await publish_snapshot(db, jid)

    # 2) 恢复：新进程完成发布
    await v.expire_lease(jid)
    m2 = out_dir / "worker-2.json"
    run_worker("organize-finish", jid, m2)
    async with v.async_session_factory() as db:
        snap_b = await publish_snapshot(db, jid)

    # 3) commit 后重投：再次投递，无重复产物、无重复 summary
    m3 = out_dir / "worker-3.json"
    run_worker("organize-finish", jid, m3)
    async with v.async_session_factory() as db:
        snap_c = await publish_snapshot(db, jid)
        n_sum = await n_calls(db, f"organize.summary:{jid}")

    no_half_publish = (
        snap_a["note_count"] == 0 and snap_a["active_note"] is None
        and snap_a["intent_count"] == 0 and snap_a["note_publish_cp"] is None
    )
    recovered_one = (
        snap_b["job_status"] == "succeeded" and snap_b["note_count"] == 1
        and snap_b["intent_count"] == 1 and snap_b["note_publish_cp"] == "succeeded"
        and snap_b["active_note"] == snap_b["output_version"]
    )
    no_dup = (
        snap_c["note_count"] == 1 and snap_c["intent_count"] == 1
        and snap_c["output_version"] == snap_b["output_version"]
        and snap_c["active_note"] == snap_b["active_note"]
    )
    ok = r1.returncode == 3 and no_half_publish and recovered_one and no_dup and n_sum == 1
    record(
        "CA-05",
        "同一job发布commit前退出→恢复1份有效笔记/Intent→重投无重复产物、无重复summary",
        ok,
        dict(
            kill_returncode=r1.returncode, before=snap_a, recovered=snap_b,
            redelivered=snap_c, summary_calls=n_sum,
        ),
    )
    STATE["ca05"] = dict(
        job_id=str(jid), before=snap_a, recovered=snap_b, redelivered=snap_c,
        summary_calls=n_sum,
    )


# ── CA-06：ASR 已知任务与结果恢复 + 发布原子性 ───────────────────────
async def ca06() -> None:
    out_dir = OUT / "ca06"
    out_dir.mkdir(parents=True, exist_ok=True)

    # (a) task_id 已存 → 恢复不重 submit
    a1, _ = await v.create_lecture(v.SUBJECT_A, "ca06-a")
    jid_a = await v.create_asr_job(a1)
    m1 = out_dir / "a-worker-1.json"
    run_worker("asr-submit-crash", jid_a, m1)
    meta1 = read_meta(m1)
    async with v.async_session_factory() as db:
        sub_cp = await v.get_checkpoint(db, jid_a, "asr.submit")
        n_sub_1 = await n_calls(db, f"asr.submit:{jid_a}")
    await v.expire_lease(jid_a)
    m2 = out_dir / "a-worker-2.json"
    run_worker("asr-finish", jid_a, m2)
    async with v.async_session_factory() as db:
        n_sub_2 = await n_calls(db, f"asr.submit:{jid_a}")
        n_poll_a = await n_calls(db, f"asr.poll:{jid_a}")
        ja = await db.get(ProcessingJob, jid_a)
    no_resubmit = (
        meta1.get("crashed") is True
        and sub_cp is not None and sub_cp.status == "succeeded"
        and n_sub_1 == 1 and n_sub_2 == 1
    )

    # (b) asr.result 已存 → 恢复不重 poll；发布事务原子提交
    b1, _ = await v.create_lecture(v.SUBJECT_A, "ca06-b")
    jid_b = await v.create_asr_job(b1)
    m3 = out_dir / "b-worker-1.json"
    run_worker("asr-result-crash", jid_b, m3)
    meta3 = read_meta(m3)
    async with v.async_session_factory() as db:
        result_cp = await v.get_checkpoint(db, jid_b, "asr.result")
        n_poll_1 = await n_calls(db, f"asr.poll:{jid_b}")
        jb1 = await db.get(ProcessingJob, jid_b)
        pub_cp_1 = await v.get_checkpoint(db, jid_b, "transcript.publish")
        ver1 = await db.get(TranscriptVersion, jb1.output_version) if jb1.output_version else None
    await v.expire_lease(jid_b)
    m4 = out_dir / "b-worker-2.json"
    run_worker("asr-finish", jid_b, m4)
    async with v.async_session_factory() as db:
        n_poll_2 = await n_calls(db, f"asr.poll:{jid_b}")
        jb2 = await db.get(ProcessingJob, jid_b)
        pub_cp_2 = await v.get_checkpoint(db, jid_b, "transcript.publish")
        ver2 = await db.get(TranscriptVersion, jb2.output_version) if jb2.output_version else None
        seg_count = (
            await db.execute(
                select(func.count()).select_from(TranscriptSegment)
                .where(TranscriptSegment.version_id == jb2.output_version)
            )
        ).scalar_one() if jb2.output_version else 0
    no_repoll = (
        meta3.get("crashed") is True
        and result_cp is not None and result_cp.status == "succeeded"
        and n_poll_1 == 1 and n_poll_2 == 1
    )
    atomic_publish = (
        jb1.output_version is None and pub_cp_1 is None and ver1 is None  # 无半发布
        and jb2.status == "succeeded" and jb2.output_version is not None
        and pub_cp_2 is not None and pub_cp_2.status == "succeeded"
        and ver2 is not None and seg_count == 3  # 版本 + 片段 + Checkpoint 同事务
    )

    # (c) 重投：succeeded 后再次投递，不产生第二份版本、不重 poll
    out_ver = jb2.output_version
    m5 = out_dir / "b-worker-3.json"
    run_worker("asr-finish", jid_b, m5)
    async with v.async_session_factory() as db:
        n_poll_3 = await n_calls(db, f"asr.poll:{jid_b}")
        jb3 = await db.get(ProcessingJob, jid_b)
    idempotent = jb3.output_version == out_ver and n_poll_3 == 1

    ok = no_resubmit and no_repoll and atomic_publish and idempotent
    record(
        "CA-06",
        "task_id已存不重submit；result已存不重poll；发布原子提交+重投幂等",
        ok,
        dict(
            no_resubmit=no_resubmit, sub_cp=sub_cp.status if sub_cp else None,
            submit_calls=n_sub_2, poll_calls_a=n_poll_a,
            no_repoll=no_repoll, result_cp=result_cp.status if result_cp else None,
            poll_calls_b=n_poll_3,
            before_publish_output_version=str(jb1.output_version) if jb1.output_version else None,
            after_output_version=str(jb2.output_version) if jb2.output_version else None,
            pub_cp=pub_cp_2.status if pub_cp_2 else None, seg_count=seg_count,
            idempotent=idempotent,
        ),
    )
    STATE["ca06"] = dict(
        submit_job=str(jid_a), submit_calls=n_sub_2, poll_calls_a=n_poll_a,
        result_job=str(jid_b), poll_calls_b=n_poll_3, seg_count=seg_count,
        output_version=str(out_ver) if out_ver else None,
    )


# ── CA-11：领取/续租/旧执行者隔离（真实进程竞争 + 60s/10s 长调用）────
async def ca11() -> None:
    out_dir = OUT / "ca11"
    out_dir.mkdir(parents=True, exist_ok=True)

    # (a) 两进程真实竞争领取同一 job：恰好一个成功
    mid, _ = await v.create_lecture(v.SUBJECT_A, "ca11-compete")
    _, vid = await v.add_transcript(mid, v.make_long_segments(n=2, base=200))
    jc = await v.make_organize_job(mid, vid)
    ma, mb = out_dir / "claim-a.json", out_dir / "claim-b.json"
    pa = spawn_worker("claim", jc, ma, owner="compete-A")
    pb = spawn_worker("claim", jc, mb, owner="compete-B")
    pa.communicate(timeout=60)
    pb.communicate(timeout=60)
    ca_meta, cb_meta = read_meta(ma), read_meta(mb)
    winners = [m for m in (ca_meta, cb_meta) if m.get("claimed")]
    async with v.async_session_factory() as db:
        jc_row = await db.get(ProcessingJob, jc)
    compete_ok = (
        len(winners) == 1 and ca_meta.get("claimed") != cb_meta.get("claimed")
        and jc_row.lease_epoch == 1 and jc_row.lease_owner == winners[0].get("owner")
    )

    # (b) 60s/10s 健康长调用 >60s：不误接管，心跳续租
    mid2, _ = await v.create_lecture(v.SUBJECT_A, "ca11-long")
    _, vid2 = await v.add_transcript(mid2, v.make_long_segments(n=2, base=200))
    jl = await v.make_organize_job(mid2, vid2)
    t0 = time.time()
    ml = out_dir / "longcall.json"
    pl = spawn_worker("organize-longcall", jl, ml, delay=65)
    await asyncio.sleep(20)  # 长调用进行中，用另一进程尝试领取
    ms = out_dir / "mid-claim.json"
    run_worker("claim", jl, ms, owner="mid-intruder")
    intruder = read_meta(ms)
    pl.communicate(timeout=180)
    long_meta = read_meta(ml)
    async with v.async_session_factory() as db:
        jl_row = await db.get(ProcessingJob, jl)
        hb = jl_row.heartbeat_at
        long_owner = jl_row.lease_owner
        long_epoch = jl_row.lease_epoch
    elapsed = time.time() - t0
    long_ok = (
        long_meta.get("failed") is not True
        and jl_row.status == "succeeded"
        and intruder.get("claimed") is False
        and hb is not None and hb.timestamp() > t0 + 40
        and elapsed >= 60
    )

    # (c) 失联到期可接管：epoch 递增
    await v.expire_lease(jc)
    mc = out_dir / "takeover.json"
    run_worker("claim", jc, mc, owner="takeover-B")
    to_meta = read_meta(mc)
    async with v.async_session_factory() as db:
        jc2 = await db.get(ProcessingJob, jc)
    takeover_ok = (
        to_meta.get("claimed") is True and jc2.lease_epoch == 2
        and jc2.lease_owner == "takeover-B"
    )

    # (d) 旧 epoch 续租/写终态均被拒，不覆盖新 owner
    old_owner = winners[0].get("owner") if winners else None  # epoch 1 的旧 owner
    msr = out_dir / "stale-renew.json"
    run_worker("stale-renew", jc, msr, owner=old_owner, epoch=1)
    sr_meta = read_meta(msr)
    async with v.async_session_factory() as db:
        jc3 = await db.get(ProcessingJob, jc)
    stale_ok = (
        sr_meta.get("renew_ok") is False and sr_meta.get("mutate_ok") is False
        and jc3.lease_owner == "takeover-B" and jc3.lease_epoch == 2
        and jc3.status == "running"  # 旧 epoch 未改写终态
    )

    ok = compete_ok and long_ok and takeover_ok and stale_ok
    record(
        "CA-11",
        "两进程竞争、60s/10s长调用不误接管、失联到期接管epoch递增、旧epoch续租/改写被拒",
        ok,
        dict(
            compete_ok=compete_ok, winners=[m.get("owner") for m in winners],
            claim_a=ca_meta.get("claimed"), claim_b=cb_meta.get("claimed"),
            long_ok=long_ok, elapsed_sec=round(elapsed, 1),
            long_owner=long_owner, long_epoch=long_epoch,
            heartbeat_at=hb.isoformat() if hb else None,
            intruder_claimed=intruder.get("claimed"),
            takeover_ok=takeover_ok, new_epoch=jc2.lease_epoch, new_owner=jc2.lease_owner,
            stale_renew_ok=sr_meta.get("renew_ok"), stale_mutate_ok=sr_meta.get("mutate_ok"),
            stale_status=jc3.status,
            longcall_meta=dict(
                pid=long_meta.get("pid"), started_at=long_meta.get("started_at"),
                finished_at=long_meta.get("finished_at"),
            ),
        ),
    )
    STATE["ca11"] = dict(
        compete_job=str(jc), long_job=str(jl), elapsed_sec=round(elapsed, 1),
        long_owner=long_owner, long_epoch=long_epoch,
        heartbeat_at=hb.isoformat() if hb else None,
        new_owner=jc2.lease_owner, new_epoch=jc2.lease_epoch,
        stale_renew_ok=sr_meta.get("renew_ok"), stale_mutate_ok=sr_meta.get("mutate_ok"),
        worker_pids=[ca_meta.get("pid"), cb_meta.get("pid"), long_meta.get("pid")],
    )


# ── 证据落盘 ─────────────────────────────────────────────────────────
async def export_provider_calls() -> list[dict]:
    async with v.async_session_factory() as db:
        rows = (
            await db.execute(
                select(ProviderCall).order_by(ProviderCall.operation_key, ProviderCall.attempt_no)
            )
        ).scalars().all()
        return [
            dict(
                operation_key=pc.operation_key, attempt_no=pc.attempt_no,
                job_id=str(pc.job_id) if pc.job_id else None,
                model=pc.model, operation_type=pc.operation_type,
                provider_task_id=pc.provider_task_id, status=pc.status,
                error_code=pc.error_code, created_at=pc.created_at.isoformat() if pc.created_at else None,
            )
            for pc in rows
        ]


def write_manifest() -> None:
    src_files = list(v.SOURCE_FILES) + ["scripts/s1_process_worker.py", "scripts/verify_s1_process.py"]
    hashes = {}
    for rel in src_files:
        p = ROOT / rel if rel.startswith("scripts/") or rel.startswith("app/") else (ROOT / rel).resolve()
        try:
            hashes[rel] = v.sha256(p)
        except Exception as e:  # noqa: BLE001
            hashes[rel] = f"missing:{e}"
    manifest = dict(
        stage="S1-independent-process",
        db=v.MAIN_DB,
        fakes=["FakeASRProvider", "FakeOrganizeModel", "DeterministicEmbedding"],
        paid_calls_enabled="false",
        source_sha256=hashes,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_results_and_report() -> None:
    (OUT / "results.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "state-before-after.json").write_text(
        json.dumps(STATE, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    passed = sum(1 for r in RESULTS if r["result"] == "通过")
    lines = [
        "# S1 独立进程证据收口报告（CA-03/04/05/06/11）",
        "",
        f"日期：{datetime.now(timezone.utc).isoformat()}（UTC）。隔离库 `{v.MAIN_DB}`，"
        "全程 fake ASR / FakeOrganizeModel / DeterministicEmbedding，未调用真实收费接口、未触碰生产库。",
        "",
        f"结果：{passed}/{len(RESULTS)} 项通过。",
        "",
        "| ID | 检查 | 结果 |",
        "| --- | --- | --- |",
    ]
    for r in RESULTS:
        lines.append(f"| {r['id']} | {r['check']} | {r['result']} |")
    lines += [
        "",
        "## 证据文件",
        "",
        "- `manifest.json`：源码 hash / 隔离库 / 替身声明。",
        "- `results.json`：逐项断言结果。",
        "- `provider-attempts.jsonl`：持久调用审计（operation_key / attempt_no / 时间 / 状态）。",
        "- `state-before-after.json`：CA-05/06/11 前后快照。",
        "- `ca03/`~`ca11/worker-*.json`：各独立进程 PID / 启动 / 退出 / owner / epoch / 心跳 / 末态。",
        "",
    ]
    (OUT / "stage-report.md").write_text("\n".join(lines), encoding="utf-8")


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    await v.create_db(v.MAIN_DB)
    v.run_alembic("head", v.asyncpg_dsn(v.MAIN_DB))
    try:
        await ca03()
        await ca04()
        await ca05()
        await ca06()
        await ca11()
    finally:
        calls = await export_provider_calls()
        (OUT / "provider-attempts.jsonl").write_text(
            "\n".join(json.dumps(c, ensure_ascii=False, default=str) for c in calls),
            encoding="utf-8",
        )
        write_manifest()
        write_results_and_report()
        for name in v.DBS:
            try:
                await v.drop_db(name)
            except Exception as e:  # noqa: BLE001
                print(f"drop {name} failed: {e}", flush=True)
    passed = sum(1 for r in RESULTS if r["result"] == "通过")
    print(f"\nS1 独立进程证据：{passed}/{len(RESULTS)} 通过，证据目录 {OUT}", flush=True)
    if passed != len(RESULTS):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
