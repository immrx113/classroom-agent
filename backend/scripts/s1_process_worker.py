"""S1 独立进程 worker（供 verify_s1_process.py 以 subprocess 启动）。

每个 worker 是独立 OS 进程：由驱动通过环境变量 ``DATABASE_URL`` 指向隔离测试库，
在一个真实进程里运行 organize / ASR 服务入口，并：

- 用「记录替身」把每次实际外发写入持久 ``provider_calls`` 表（跨进程可复算）；
- 按角色注入固定故障（分块后退出 / 汇总后退出 / 发布前失败 / submit/result 后失联 / 长调用）；
- 把进程元数据（PID、启动/退出时间、owner/epoch/heartbeat、任务末态）写入 ``--meta`` JSON。

本脚本只被测试驱动调用，不接真实收费接口。
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("DASHSCOPE_API_KEY", "")
os.environ.setdefault("INDEX_EMBEDDING_PROVIDER", "deterministic")
os.environ.setdefault("CLASSROOM_PAID_CALLS_ENABLED", "false")
os.environ.setdefault("PYTHONUTF8", "1")
# DATABASE_URL 由驱动通过环境变量注入，指向隔离测试库（不触碰生产库）。

from app.db.session import async_session_factory  # noqa: E402
from app.models.processing_job import ProcessingJob  # noqa: E402
from app.services import lease_service  # noqa: E402
import app.services.job_service as job_service  # noqa: E402
import app.services.organize_service as organize_service  # noqa: E402
from app.services.asr_provider import FakeASRProvider, ProviderError  # noqa: E402
from app.services.checkpoint_service import (  # noqa: E402
    count_provider_calls,
    record_provider_call,
)
from app.services.organize_model import FakeOrganizeModel, ModelError, WorkerCrash  # noqa: E402


def now() -> datetime:
    return datetime.now(timezone.utc)


async def _record(
    operation_key: str,
    job_id: uuid.UUID,
    *,
    model: str | None = None,
    operation_type: str | None = None,
    status: str = "ok",
    error_code: str | None = None,
    provider_task_id: str | None = None,
) -> None:
    """用独立 session 记录一次实际外发 attempt（先 commit，随后续主事务回滚也不丢）。"""
    async with async_session_factory() as db:
        attempt = (await count_provider_calls(db, operation_key)) + 1
        await record_provider_call(
            db,
            operation_key=operation_key,
            attempt_no=attempt,
            job_id=job_id,
            model=model,
            operation_type=operation_type,
            status=status,
            error_code=error_code,
            provider_task_id=provider_task_id,
        )
        await db.commit()


class RecordingOrganizeModel(FakeOrganizeModel):
    """记录每次实际 extract / summarize 到 provider_calls，并注入固定故障。"""

    def __init__(self, job_id: uuid.UUID, *, crash_on_chunk=None, summarize_delay: float = 0.0):
        super().__init__()
        self.job_id = job_id
        self.crash_on_chunk = crash_on_chunk
        self.summarize_delay = summarize_delay

    async def extract_chunk(self, chunk):
        key = f"organize.extract:{self.job_id}:{chunk.chunk_index}"
        try:
            out = await super().extract_chunk(chunk)
        except WorkerCrash:
            raise  # 故障注入发生在实际调用之前，不记为外发
        except ModelError as e:
            await _record(key, self.job_id, model=self.name, operation_type="extract",
                          status="error", error_code=e.code)
            raise
        await _record(key, self.job_id, model=self.name, operation_type="extract", status="ok")
        return out

    async def summarize(self, conclusions, tasks, meta):
        key = f"organize.summary:{self.job_id}"
        if self.summarize_delay:
            await asyncio.sleep(self.summarize_delay)
        try:
            out = await super().summarize(conclusions, tasks, meta)
        except ModelError as e:
            await _record(key, self.job_id, model=self.name, operation_type="summarize",
                          status="error", error_code=e.code)
            raise
        await _record(key, self.job_id, model=self.name, operation_type="summarize", status="ok")
        return out


class RecordingASRProvider(FakeASRProvider):
    """记录每次实际 submit / poll 到 provider_calls。"""

    def __init__(self, job_id: uuid.UUID):
        super().__init__()
        self.job_id = job_id

    async def submit(self, local_path):
        key = f"asr.submit:{self.job_id}"
        try:
            out = await super().submit(local_path)
        except ProviderError as e:
            await _record(key, self.job_id, model=self.name, operation_type="asr_submit",
                          status="error", error_code=e.code)
            raise
        await _record(key, self.job_id, model=self.name, operation_type="asr_submit",
                      status="ok", provider_task_id=out.task_id)
        return out

    async def poll(self, task_id):
        key = f"asr.poll:{self.job_id}"
        try:
            out = await super().poll(task_id)
        except ProviderError as e:
            await _record(key, self.job_id, model=self.name, operation_type="asr_poll",
                          status="error", error_code=e.code)
            raise
        await _record(key, self.job_id, model=self.name, operation_type="asr_poll", status="ok")
        return out


def _crash():
    raise WorkerCrash()


def _worker_lost():
    raise job_service.WorkerLost()


def _publish_fail():
    raise RuntimeError("publish interrupted (commit 前故障注入)")


def _publish_kill():
    # 模拟发布事务 commit 前进程退出（os._exit 不触发 except/finally，进程直接死亡，
    # 未提交的 note/active/IndexIntent 随连接关闭回滚，不留半发布）。
    os._exit(3)


async def _snapshot(job_id: uuid.UUID) -> dict:
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, job_id)
        if j is None:
            return {}
        return {
            "status": j.status,
            "lease_owner": j.lease_owner,
            "lease_epoch": j.lease_epoch,
            "lease_expires_at": j.lease_expires_at.isoformat() if j.lease_expires_at else None,
            "heartbeat_at": j.heartbeat_at.isoformat() if j.heartbeat_at else None,
            "provider_task_id": j.provider_task_id,
            "output_version": str(j.output_version) if j.output_version else None,
            "error_code": j.error_code,
            "error_type": j.error_type,
        }


async def _run(role: str, job_id: uuid.UUID, meta: dict, args) -> None:
    if role == "organize-crash18":
        model = RecordingOrganizeModel(job_id, crash_on_chunk=args.crash_chunk)
        try:
            async with async_session_factory() as db:
                await organize_service.organize_service.run_organize_job(db, job_id, model=model)
        except WorkerCrash:
            meta["crashed"] = True
    elif role == "organize-finish":
        model = RecordingOrganizeModel(job_id)
        async with async_session_factory() as db:
            await organize_service.organize_service.run_organize_job(db, job_id, model=model)
    elif role == "organize-summary-crash":
        organize_service._post_summary_hook = _crash
        model = RecordingOrganizeModel(job_id)
        try:
            async with async_session_factory() as db:
                await organize_service.organize_service.run_organize_job(db, job_id, model=model)
        except WorkerCrash:
            meta["crashed"] = True
        finally:
            organize_service._post_summary_hook = None
    elif role == "organize-publish-fail":
        organize_service._publish_hook = _publish_fail
        model = RecordingOrganizeModel(job_id)
        try:
            async with async_session_factory() as db:
                await organize_service.organize_service.run_organize_job(db, job_id, model=model)
        finally:
            organize_service._publish_hook = None
    elif role == "organize-publish-kill":
        # 发布事务 commit 前进程退出（os._exit 不会返回，start 元数据已落盘）
        organize_service._publish_hook = _publish_kill
        model = RecordingOrganizeModel(job_id)
        async with async_session_factory() as db:
            await organize_service.organize_service.run_organize_job(db, job_id, model=model)
    elif role == "stale-renew":
        # 旧 owner/epoch 在被接管后尝试续租/写终态：两者都应失败（CA-11 隔离）
        owner = args.owner
        epoch = int(args.epoch)
        async with async_session_factory() as db:
            meta["renew_ok"] = await lease_service.renew_lease(db, job_id, owner, epoch)
            await db.rollback()

            def _mut(row: ProcessingJob) -> None:
                row.status = "failed"
                row.error_code = "stale_write"

            meta["mutate_ok"] = await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _mut)
            await db.rollback()
    elif role == "asr-submit-crash":
        job_service._submit_committed_hook = _worker_lost
        provider = RecordingASRProvider(job_id)
        try:
            async with async_session_factory() as db:
                await job_service.run_job(db, job_id, provider=provider)
        except job_service.WorkerLost:
            meta["crashed"] = True
        finally:
            job_service._submit_committed_hook = None
    elif role == "asr-result-crash":
        job_service._result_committed_hook = _worker_lost
        provider = RecordingASRProvider(job_id)
        try:
            async with async_session_factory() as db:
                await job_service.run_job(db, job_id, provider=provider)
        except job_service.WorkerLost:
            meta["crashed"] = True
        finally:
            job_service._result_committed_hook = None
    elif role == "asr-finish":
        provider = RecordingASRProvider(job_id)
        async with async_session_factory() as db:
            await job_service.run_job(db, job_id, provider=provider)
    elif role == "organize-longcall":
        model = RecordingOrganizeModel(job_id, summarize_delay=args.delay)
        async with async_session_factory() as db:
            await organize_service.organize_service.run_organize_job(db, job_id, model=model)
    elif role == "claim":
        owner = args.owner or f"worker-{uuid.uuid4().hex[:12]}"
        async with async_session_factory() as db:
            claimed, epoch = await lease_service.claim_job(db, job_id, owner)
            await db.commit()
        meta["owner"] = owner
        meta["claimed"] = claimed
        meta["epoch"] = epoch
    else:
        raise SystemExit(f"unknown role: {role}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--crash-chunk", type=int, default=18)
    ap.add_argument("--delay", type=float, default=65.0)
    ap.add_argument("--owner", default=None)
    ap.add_argument("--epoch", type=int, default=0)
    args = ap.parse_args()

    job_id = uuid.UUID(args.job_id)
    meta = {
        "role": args.role,
        "pid": os.getpid(),
        "job_id": str(job_id),
        "started_at": now().isoformat(),
        "crashed": False,
        "error": None,
    }

    def _write() -> None:
        with open(args.meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    # 先落 start 元数据：os._exit 型故障不触发 finally，仍留有进程身份证据。
    _write()
    try:
        await _run(args.role, job_id, meta, args)
    except Exception as e:  # noqa: BLE001
        meta["error"] = repr(e)
        meta["failed"] = True
    finally:
        try:
            meta["snapshot"] = await _snapshot(job_id)
        except Exception as e:  # noqa: BLE001
            meta["snapshot_error"] = repr(e)
        meta["finished_at"] = now().isoformat()
        _write()
    return 1 if meta.get("failed") else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
