r"""专项「Checkpoint + AI 调用控制」分阶段冻结验收（v1.1）自动验证。

针对 docs/classroom-agent/acceptance/checkpoint-ai-control-frozen-checklist-v1.md。
支持 ``--stage S1|S2|S3|S4`` 选择阶段、``--test CA-xx`` 选择单项复验，
``--out <dir>`` 指定证据目录。S1 覆盖 CA-01~06、CA-08~09、CA-11（9 项 P0）。

运行（backend 目录）：
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  .\.venv\Scripts\python.exe scripts/verify_checkpoint_ai_control.py --stage S1

自动验收不调用真实收费接口：全程使用 fake ASR / FakeOrganizeModel /
DeterministicEmbedding 替身，数据库为隔离测试库，绝不触碰生产库 yuan_meet。
"""

import argparse
import asyncio
import hashlib
import json
import os
import random
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.update(
    OPENAI_API_KEY="", DASHSCOPE_API_KEY="", PYTHONPATH=".", PYTHONUTF8="1",
    INDEX_EMBEDDING_PROVIDER="deterministic", CLASSROOM_PAID_CALLS_ENABLED="false",
    RATE_LIMIT_ENABLED="false",
)

import asyncpg
from app.config import settings

# S2 使用独立 Redis 库（db 15），绝不清空用户默认 db 0；死端口用于模拟 Redis 不可用（CA-16）。
TEST_REDIS_URL = "redis://localhost:6379/15"
DEAD_REDIS_URL = "redis://localhost:6399/0"

BASE = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
PREFIX = "yuan_checkpoint_test_" + uuid.uuid4().hex[:10] + "_"
MAIN_DB = PREFIX + "main"

# 在导入会创建引擎的模块前，把引擎指向隔离测试库，绝不触碰生产库 yuan_meet。
settings.DATABASE_URL = BASE.replace("postgresql://", "postgresql+asyncpg://") + "/" + MAIN_DB
os.environ["DATABASE_URL"] = settings.DATABASE_URL

from app.db.session import async_session_factory  # noqa: E402
from app.models.audio_asset import AudioAsset  # noqa: E402
from app.models.course import Course  # noqa: E402
from app.models.index_version import IndexBlock, IndexVersion  # noqa: E402
from app.models.meeting import Meeting  # noqa: E402
from app.models.note_version import NoteVersion  # noqa: E402
from app.models.organize import ChunkItem, ChunkManifest, IndexIntent  # noqa: E402
from app.models.budget import BudgetAccount, BudgetReservation, ProviderPermitSlot  # noqa: E402
from app.models.processing_job import JobCheckpoint, OutboxEvent, ProcessingJob, ProviderCall  # noqa: E402
from app.models.source_document import SourceDocument, SourceDocumentVersion  # noqa: E402
from app.models.transcript_version import TranscriptSegment, TranscriptVersion  # noqa: E402
from app.services import lease_service  # noqa: E402
from app.services import rate_limit_service as rl  # noqa: E402
import app.services.job_service as job_service  # noqa: E402
import app.services.index_service as index_service  # noqa: E402
import app.services.organize_service as organize_service  # noqa: E402
from app.services import call_budget_service as budget  # noqa: E402
from app.services import provider_permit as permit  # noqa: E402
from app.services import backoff as backoff_mod  # noqa: E402
from app.services.ai_call_gateway import (  # noqa: E402
    CallSpec,
    ControlWaitError,
    call_controlled,
    operation_deadline,
    run_operation,
)
import app.services.ai_call_gateway as gateway  # noqa: E402
from app.services.asr_provider import FakeASRProvider, ProviderError  # noqa: E402
from app.services.checkpoint_service import (  # noqa: E402
    compute_idempotency_key,
    get_checkpoint,
    policy_version,
    sha256_hex,
)
from app.services.embedding_service import DeterministicEmbedding, EmbeddingProvider  # noqa: E402
from app.services.errors import BudgetExhaustedError, ProviderUnknownError, ServiceError  # noqa: E402
from app.services.errors import ERR_RATE_LIMIT  # noqa: E402
from app.services.organize_model import FakeOrganizeModel, ModelError, WorkerCrash  # noqa: E402
from app.services.scope import ScopeContext  # noqa: E402
from sqlalchemy import delete, func, select, update  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]  # backend/
EVIDENCE_DIR = ROOT.parent / "docs" / "classroom-agent" / "acceptance" / "evidence-checkpoint-ai-control-20261030"

SUBJECT_A = "subject-a"
SUBJECT_B = "subject-b"
DBS: list[str] = [MAIN_DB]
RESULTS: list[dict] = []


def record(pid: str, check: str, ok: bool, detail) -> None:
    row = dict(id=pid, check=check, result="通过" if ok else "不通过", detail=detail)
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


# 源码快照：绑定本轮实现、迁移、脚本与冻结清单（证据口径，不记录密钥/正文）
SOURCE_FILES = [
    "app/services/checkpoint_service.py",
    "app/services/lease_service.py",
    "app/services/organize_service.py",
    "app/services/organize_model.py",
    "app/services/job_service.py",
    "app/services/asr_provider.py",
    "app/services/dashscope_asr_service.py",
    "app/services/oss_service.py",
    "app/services/index_service.py",
    "app/services/embedding_service.py",
    "app/services/transcript_service.py",
    "app/models/processing_job.py",
    "app/config.py",
    "app/services/rate_limit_service.py",
    "app/services/ai_call_gateway.py",
    "app/services/call_budget_service.py",
    "app/services/provider_permit.py",
    "app/services/backoff.py",
    "app/models/budget.py",
    "app/api/jobs.py",
    "app/api/diagnostics.py",
    "app/schemas/job.py",
    "app/scripts/token_bucket.lua",
    "alembic/versions/2026_10_30_0001-a9b8c7d6e5f4_add_checkpoint_ai_control.py",
    "alembic/versions/2026_10_30_0002-b9c8d7e6f5a4_add_call_gateway_budget.py",
    "alembic/versions/2026_10_30_0003-e6f7a8b9c0d2_add_provider_call_result_ref.py",
    "alembic/versions/2026_10_31_0001-f1a2b3c4d5e6_add_subject_audit_and_reuse.py",
    "app/services/access.py",
    "app/services/audio_asset_service.py",
    "app/api/lectures.py",
    "app/api/notes.py",
    "app/api/courses.py",
    "app/api/documents.py",
    "scripts/verify_checkpoint_ai_control.py",
    "scripts/s2_redis_worker.py",
    "scripts/s3_budget_worker.py",
    "../docs/classroom-agent/acceptance/checkpoint-ai-control-frozen-checklist-v1.md",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def pg_dsn(name: str) -> str:
    return BASE + "/" + name


def asyncpg_dsn(name: str) -> str:
    return BASE.replace("postgresql://", "postgresql+asyncpg://") + "/" + name


def now() -> datetime:
    return datetime.now(timezone.utc)


async def create_db(name: str) -> None:
    conn = await asyncpg.connect(BASE + "/postgres", timeout=5)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    conn = await asyncpg.connect(pg_dsn(name))
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()


async def drop_db(name: str) -> None:
    conn = await asyncpg.connect(BASE + "/postgres", timeout=5)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


def run_alembic(revision: str, url: str) -> None:
    env = dict(os.environ)
    env.update(
        DATABASE_URL=url, PYTHONPATH=".", PYTHONUTF8="1",
        OPENAI_API_KEY="", DASHSCOPE_API_KEY="",
    )
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        env=env, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade {revision} failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        )


def run_alembic_downgrade(revision: str, url: str) -> None:
    """回退到指定 revision（CA-25 停写只读回退验证）。"""
    env = dict(os.environ)
    env.update(
        DATABASE_URL=url, PYTHONPATH=".", PYTHONUTF8="1",
        OPENAI_API_KEY="", DASHSCOPE_API_KEY="",
    )
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", revision],
        env=env, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"alembic downgrade {revision} failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        )


# ── 数据构造助手 ──────────────────────────────────────────────────────


async def create_lecture(owner_id: str, title: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with async_session_factory() as db:
        course = Course(owner_id=owner_id, title=f"course-{title}")
        db.add(course)
        await db.flush()
        meeting = Meeting(course_id=course.id, title=title, status="pending")
        db.add(meeting)
        await db.flush()
        mid, cid = meeting.id, course.id
        await db.commit()
        return mid, cid


def _seg(seq, content, start_ms=None, end_ms=None) -> dict:
    return dict(seq_index=seq, speaker="t", content=content,
                start_ms=start_ms, end_ms=end_ms)


async def add_transcript(
    lecture_id: uuid.UUID, segments: list[dict], *, is_demo: bool = False, active: bool = True
) -> tuple[uuid.UUID, uuid.UUID]:
    async with async_session_factory() as db:
        meeting = await db.get(Meeting, lecture_id)
        asset = AudioAsset(
            lecture_id=lecture_id, storage_key="assets/test.wav",
            duration_ms=max((s.get("end_ms") or 0) for s in segments) or None,
            is_demo=is_demo,
        )
        db.add(asset)
        await db.flush()
        version = TranscriptVersion(
            audio_asset_id=asset.id, lecture_id=lecture_id, version=1,
            provider="fake", status="building",
        )
        db.add(version)
        await db.flush()
        for s in segments:
            db.add(
                TranscriptSegment(
                    version_id=version.id, seq_index=s["seq_index"],
                    speaker=s.get("speaker"), content=s["content"],
                    start_ms=s.get("start_ms"), end_ms=s.get("end_ms"),
                )
            )
        await db.flush()
        if active:
            if meeting.active_transcript_version_id is not None:
                old = await db.get(TranscriptVersion, meeting.active_transcript_version_id)
                if old is not None and old.id != version.id:
                    old.status = "superseded"
            meeting.active_audio_id = asset.id
            meeting.active_transcript_version_id = version.id
            meeting.status = "processed"
            version.status = "active"
        else:
            version.status = "ready"
        await db.commit()
        return asset.id, version.id


def make_long_segments(n: int = 20, base: int = 2000) -> list[dict]:
    """n 个长片段：每个 base 字符，两个片段之和超过 input_budget(3000) → 每片段一个块。"""
    return [
        _seg(i, f"K{i:02d}-" + "字" * base, start_ms=i * 5000, end_ms=(i + 1) * 5000 - 100)
        for i in range(n)
    ]


async def make_organize_job(lecture_id: uuid.UUID, version_id: uuid.UUID) -> uuid.UUID:
    async with async_session_factory() as db:
        job = await organize_service.organize_service.create_organize_job(
            db, ScopeContext(subject_id=SUBJECT_A),
            lecture_id=lecture_id, transcript_version_id=version_id,
        )
        return job.id


async def create_asr_job(lecture_id: uuid.UUID, subject: str = SUBJECT_A) -> uuid.UUID:
    async with async_session_factory() as db:
        asset = AudioAsset(
            lecture_id=lecture_id, storage_key="assets/test.wav",
            duration_ms=10000, is_demo=False,
        )
        db.add(asset)
        await db.flush()
        job = ProcessingJob(
            type="asr", resource_id=lecture_id, input_version=asset.id,
            config_hash="asr-cfg", subject_id=subject,
            status="queued", stage="created", attempt=0, cancel_requested=False,
        )
        db.add(job)
        await db.flush()
        db.add(OutboxEvent(
            business_key=f"job:{job.id}", event_type="asr_requested", job_id=job.id,
            payload={"job_id": str(job.id)}, status="pending", attempt=0,
        ))
        await db.commit()
        return job.id


async def expire_lease(job_id: uuid.UUID) -> None:
    """模拟旧 worker 失联：租约过期，允许新执行者接管。"""
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, job_id)
        j.lease_expires_at = now() - timedelta(seconds=10)
        await db.commit()


def _boom_worker_crash():
    raise WorkerCrash()


def _boom_worker_lost():
    raise job_service.WorkerLost()


class CountingEmbed(EmbeddingProvider):
    """确定性向量 + 调用计数（CA-09 证明前两单元不重复 embed）。"""
    model = "fake-deterministic"
    name = "fake-deterministic"

    def __init__(self):
        self.dimensions = 1024
        self.calls = 0
        self._inner = DeterministicEmbedding(1024)

    async def embed_batch(self, texts):
        self.calls += len(texts)
        return await self._inner.embed_batch(texts)


class CrashEmbed(CountingEmbed):
    """第 crash_after 次之后 embed 抛 BuildInterrupted，模拟提交若干单元后 worker 退出。"""

    def __init__(self, crash_after: int):
        super().__init__()
        self.crash_after = crash_after

    async def embed_batch(self, texts):
        self.calls += len(texts)
        if self.calls > self.crash_after:
            raise index_service.BuildInterrupted()
        return await self._inner.embed_batch(texts)


class SlowASRProvider(FakeASRProvider):
    """poll 睡眠 delay 秒，模拟超过租约时长的健康长调用（CA-11 独立续租诊断）。"""

    def __init__(self, delay: float = 1.5):
        super().__init__()
        self.delay = delay

    async def poll(self, task_id: str) -> list[dict]:
        await asyncio.sleep(self.delay)
        return await super().poll(task_id)


class SlowEmbed(CountingEmbed):
    """embed 睡眠 delay 秒，模拟超过租约时长的健康长向量化（CA-11 独立续租诊断）。"""

    def __init__(self, delay: float = 1.5):
        super().__init__()
        self.delay = delay

    async def embed_batch(self, texts):
        await asyncio.sleep(self.delay)
        return await super().embed_batch(texts)


class TakeoverExtractModel(FakeOrganizeModel):
    """extract 外发期间：租约过期 + 另一 session 接管（CA-11 迟到响应隔离）。"""

    def __init__(self, job_id: uuid.UUID, replacement_owner: str):
        super().__init__()
        self.job_id = job_id
        self.replacement_owner = replacement_owner
        self.done = False

    async def extract_chunk(self, chunk):
        if not self.done:
            self.done = True
            async with async_session_factory() as db:
                j = await db.get(ProcessingJob, self.job_id)
                j.lease_expires_at = now() - timedelta(seconds=10)
                await db.commit()
            async with async_session_factory() as db:
                await lease_service.claim_job(db, self.job_id, self.replacement_owner)
                await db.commit()
        return await super().extract_chunk(chunk)


class TakeoverEmbed(CountingEmbed):
    """embed 外发期间：租约过期 + 另一 session 接管（CA-11 迟到响应隔离）。"""

    def __init__(self, job_id: uuid.UUID, replacement_owner: str):
        super().__init__()
        self.job_id = job_id
        self.replacement_owner = replacement_owner
        self.done = False

    async def embed_batch(self, texts):
        if not self.done:
            self.done = True
            async with async_session_factory() as db:
                j = await db.get(ProcessingJob, self.job_id)
                j.lease_expires_at = now() - timedelta(seconds=10)
                await db.commit()
            async with async_session_factory() as db:
                await lease_service.claim_job(db, self.job_id, self.replacement_owner)
                await db.commit()
        return await super().embed_batch(texts)


class TakeoverErrorASRProvider(FakeASRProvider):
    """poll 外发期间：租约过期 + 另一 session 接管，随后返回可重试 ProviderError
    （CA-11 迟到异常隔离：旧执行者不得把新 owner 改写成 retry_wait/provider_error）。"""

    def __init__(self, job_id: uuid.UUID, replacement_owner: str):
        super().__init__()
        self.job_id = job_id
        self.replacement_owner = replacement_owner
        self.done = False

    async def poll(self, task_id: str) -> list[dict]:
        if not self.done:
            self.done = True
            async with async_session_factory() as db:
                j = await db.get(ProcessingJob, self.job_id)
                j.lease_expires_at = now() - timedelta(seconds=10)
                await db.commit()
            async with async_session_factory() as db:
                await lease_service.claim_job(db, self.job_id, self.replacement_owner)
                await db.commit()
        raise ProviderError("rate_limit", "轮询限流", retryable=True, code="rate_limited")


class TakeoverErrorModel(FakeOrganizeModel):
    """summarize 外发期间：租约过期 + 另一 session 接管，随后返回可重试 ModelError
    （CA-11 迟到异常隔离：旧执行者不得把新 owner 改写成 failed/model_error）。"""

    def __init__(self, job_id: uuid.UUID, replacement_owner: str):
        super().__init__()
        self.job_id = job_id
        self.replacement_owner = replacement_owner
        self.done = False

    async def summarize(self, conclusions, tasks, meta):
        if not self.done:
            self.done = True
            async with async_session_factory() as db:
                j = await db.get(ProcessingJob, self.job_id)
                j.lease_expires_at = now() - timedelta(seconds=10)
                await db.commit()
            async with async_session_factory() as db:
                await lease_service.claim_job(db, self.job_id, self.replacement_owner)
                await db.commit()
        raise ModelError("provider_error", "最终汇总失败", retryable=True)


class UnknownEmbed(EmbeddingProvider):
    """向量化首次外发即结果未知（CA-07 embedding 未知窗口）。"""
    model = "fake-unknown"
    name = "fake-unknown"

    def __init__(self):
        self.dimensions = 1024
        self.calls = 0

    async def embed_batch(self, texts):
        self.calls += len(texts)
        raise ProviderUnknownError("embedding 已发出但结果未知")


class RetryOncePollProvider(FakeASRProvider):
    """poll 首次限流、之后成功（CA-15 注入重试：每次 attempt 独立受控记录）。"""

    def __init__(self):
        super().__init__()
        self.failures_left = 1
        self.transport_calls = 0

    async def poll(self, task_id: str) -> list[dict] | None:
        # transport 真实调用计数（含被网关重试的首次限流调用，不受基类 poll_calls 干扰）
        self.transport_calls += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            raise ProviderError("rate_limited", "轮询限流", retryable=True, code="rate_limited")
        return await super().poll(task_id)


class RetryOnceExtractModel(FakeOrganizeModel):
    """指定块首次明确可重试失败、之后成功（CA-26 一次可恢复失败）。

    首次失败由网关 call_controlled 在 attempt 1 记录后按有界退避重试，
    attempt 2 成功——只补这一块的缺失，不重发已成功块。
    """

    def __init__(self, fail_chunk: int):
        super().__init__()
        self.fail_chunk = fail_chunk
        self.failed = False

    async def extract_chunk(self, chunk):
        if chunk.chunk_index == self.fail_chunk and not self.failed:
            self.failed = True
            raise ModelError("provider_error", "块调用限流", retryable=True, code="rate_limited")
        return await super().extract_chunk(chunk)


class RetryAfterProviderError(Exception):
    """携带超过期限 Retry-After 的可重试错误（CA-20 超期停止）。"""
    retryable = True

    def __init__(self):
        self.error_type = "rate_limited"
        self.retry_after_seconds = 1000


class _ZeroRng:
    """确定性 RNG：uniform 恒 0，退避立即重试，避免测试睡眠（CA-20/CA-21）。"""

    def uniform(self, a, b):
        return 0.0


class _AlwaysFailTransport:
    """连续明确临时失败的 transport（CA-21 总尝试上限 / CA-23 显式新操作）。"""

    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        raise ProviderError("provider_error", "供应商内部错误", retryable=True)


# ── S3 助手：预算/预留/permit 状态、网关直调、隔离重置 ───────────────

async def _flush_redis() -> None:
    pool = await rl._get_pool()
    await pool.flushdb()


async def _s3_reset() -> None:
    """重置预算账户/预留 + permit 槽位 + Redis 桶，隔离子场景（CA-18/19 累计不串扰）。"""
    async with async_session_factory() as db:
        await db.execute(delete(BudgetReservation))
        await db.execute(delete(BudgetAccount))
        await db.execute(update(ProviderPermitSlot).values(
            job_id=None, lease_owner=None, lease_epoch=0, lease_expires_at=None, operation_key=None,
        ))
        await db.commit()
    await _flush_redis()


async def _budget_state(subject_id: str = SUBJECT_A, day=None) -> dict:
    async with async_session_factory() as db:
        return await budget.available_budget(db, subject_id, day)


async def _pc_rows(operation_key: str) -> list[dict]:
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(ProviderCall)
                .where(ProviderCall.operation_key == operation_key)
                .order_by(ProviderCall.attempt_no)
            )
        ).scalars().all()
        return [
            dict(status=r.status, attempt_no=r.attempt_no, reserved_amount=r.reserved_amount,
                 settled_amount=r.settled_amount, usage_state=r.usage_state, error_code=r.error_code)
            for r in rows
        ]


async def _pc_count(operation_key_prefix: str) -> int:
    async with async_session_factory() as db:
        return (
            await db.execute(
                select(func.count(ProviderCall.id)).where(
                    ProviderCall.operation_key.like(f"{operation_key_prefix}%")
                )
            )
        ).scalar_one()


async def _occupy_permits(n: int) -> list[tuple[int, str]]:
    """直接抢占 n 个 permit 槽位（CA-17：在途 permit ≤ 2 时第 3 任务等待）。"""
    held: list[tuple[int, str]] = []
    async with async_session_factory() as db:
        for i in range(n):
            slot = await permit.acquire_permit(
                db, job_id=uuid.uuid4(), owner=f"holder-{i}", epoch=1, operation_key=f"hold:{i}"
            )
            if slot is None:
                raise AssertionError(f"仅抢占 {i}/{n} 个 permit，槽位不足")
            await db.commit()
            held.append((slot, f"holder-{i}"))
    return held


async def _set_account(kind: str, subject_id: str, *, reserved_micro=None, settled_micro=None) -> None:
    """直接设置账户累计预留/结算（CA-18 制造「最后额度」）。"""
    async with async_session_factory() as db:
        day = budget.shanghai_day()
        await budget._ensure_accounts(db, subject_id, day)
        row = (
            await db.execute(
                select(BudgetAccount).where(
                    BudgetAccount.kind == kind,
                    BudgetAccount.account_key == budget._account_key(kind, subject_id),
                    BudgetAccount.day == budget._day_for(kind, day),
                )
            )
        ).scalars().first()
        if reserved_micro is not None:
            row.reserved_micro = reserved_micro
        if settled_micro is not None:
            row.settled_micro = settled_micro
        await db.commit()


async def _reserve_once(subject_id: str, operation_key: str, attempt_no: int) -> str:
    """独立 session 预留一次；预算不足返回 'exhausted'（CA-18 并发竞争）。"""
    async with async_session_factory() as db:
        try:
            await budget.reserve_budget(
                db, attempt_id=uuid.uuid4(), operation_key=operation_key,
                attempt_no=attempt_no, subject_id=subject_id,
            )
            await db.commit()
            return "ok"
        except BudgetExhaustedError:
            await db.rollback()
            return "exhausted"


def _launch_budget_worker(subject_id: str, operation_key: str, attempt_no: int):
    """启动独立进程 budget worker（CA-18 两独立进程抢最后额度）。"""
    env = dict(os.environ)
    env.update(
        DATABASE_URL=settings.DATABASE_URL, PYTHONPATH=".", PYTHONUTF8="1",
        OPENAI_API_KEY="", DASHSCOPE_API_KEY="",
    )
    return subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "s3_budget_worker.py"),
         "--subject", subject_id, "--operation-key", operation_key,
         "--attempt-no", str(attempt_no)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", cwd=str(ROOT),
    )


def _worker_result(proc) -> str:
    out, err = proc.communicate(timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"s3_budget_worker failed: {proc.returncode}\nSTDOUT:{out}\nSTDERR:{err}")
    return out.strip()


# ── CA-01 任务身份与并发创建 ──────────────────────────────────────────
async def ca_01(current):
    l1, _ = await create_lecture(SUBJECT_A, "ca01-L1")
    _, v1 = await add_transcript(l1, make_long_segments(n=2, base=200))

    # (a) 同主体/资源/输入/语义配置并发创建 10 次 → 仅 1 个 job + 1 个初始 outbox
    async def _create_once():
        return await make_organize_job(l1, v1)

    ids = await asyncio.gather(*[_create_once() for _ in range(10)])
    async with async_session_factory() as db:
        jobs = (await db.execute(
            select(ProcessingJob).where(ProcessingJob.type == "organize",
                                        ProcessingJob.resource_id == l1)
        )).scalars().all()
        outbox = (await db.execute(
            select(OutboxEvent).where(OutboxEvent.job_id == ids[0])
        )).scalars().all()
    same_key = len(set(ids)) == 1
    one_job = len(jobs) == 1 and len(outbox) == 1

    # (b) 输入 V2 → 独立任务
    _, v2 = await add_transcript(l1, make_long_segments(n=3, base=200))
    j2 = await make_organize_job(l1, v2)
    async with async_session_factory() as db:
        jobs2 = (await db.execute(
            select(ProcessingJob).where(ProcessingJob.type == "organize",
                                        ProcessingJob.resource_id == l1)
        )).scalars().all()
    input_isolated = j2 != ids[0] and len(jobs2) == 2

    # (c) 语义配置变化 → 独立任务
    orig_overlap = settings.ORGANIZE_CHUNK_OVERLAP
    settings.ORGANIZE_CHUNK_OVERLAP = 2
    try:
        j3 = await make_organize_job(l1, v1)
    finally:
        settings.ORGANIZE_CHUNK_OVERLAP = orig_overlap
    async with async_session_factory() as db:
        jobs3 = (await db.execute(
            select(ProcessingJob).where(ProcessingJob.type == "organize",
                                        ProcessingJob.resource_id == l1)
        )).scalars().all()
    config_isolated = j3 != ids[0] and j3 != j2 and len(jobs3) == 3

    # (d) 仅调整整理重试参数（ORGANIZE_MAX_ATTEMPTS，留 policy_version）不改变语义身份、
    # 不新建任务、不废弃成功产物（CA-01：策略不改变语义身份）
    hash_before = organize_service._organize_config_hash()
    orig_attempts = settings.ORGANIZE_MAX_ATTEMPTS
    settings.ORGANIZE_MAX_ATTEMPTS = 5
    try:
        hash_retry = organize_service._organize_config_hash()
        j4 = await make_organize_job(l1, v1)
    finally:
        settings.ORGANIZE_MAX_ATTEMPTS = orig_attempts
    async with async_session_factory() as db:
        jobs4 = (await db.execute(
            select(ProcessingJob).where(ProcessingJob.type == "organize",
                                        ProcessingJob.resource_id == l1)
        )).scalars().all()
    policy_isolated = (
        j4 == ids[0] and len(jobs4) == 3 and hash_before == hash_retry
    )

    # (e) prompt/schema 版本纳入语义身份：改提示词版本 → 语义哈希变化（旧任务仍固定旧身份）
    orig_prompt = organize_service.ORGANIZE_PROMPT_VERSION
    organize_service.ORGANIZE_PROMPT_VERSION = "organize-v2"
    try:
        prompt_hash = organize_service._organize_config_hash()
    finally:
        organize_service.ORGANIZE_PROMPT_VERSION = orig_prompt
    prompt_identity = prompt_hash != hash_before

    record(
        "CA-01",
        "并发创建同身份仅1 job + 改输入/语义配置独立任务 + 改重试参数不改业务键 + prompt版本纳入身份",
        same_key and one_job and input_isolated and config_isolated and policy_isolated and prompt_identity,
        dict(same_key=same_key, one_job=one_job, input_isolated=input_isolated,
             config_isolated=config_isolated, policy_isolated=policy_isolated,
             hash_before=hash_before, hash_retry=hash_retry,
             prompt_identity=prompt_identity, prompt_hash=prompt_hash,
             job_count_after=len(jobs4)),
    )


# ── CA-02 固定输入与 Checkpoint 有效性 ────────────────────────────────
async def ca_02(current):
    # (a) 固定输入：V1 任务在 V2 成为当前后仍使用 V1
    l1, _ = await create_lecture(SUBJECT_A, "ca02-L1")
    _, v1 = await add_transcript(l1, [
        _seg(0, "V1-ONLY-ANCHOR", start_ms=0, end_ms=1000),
        _seg(1, "V1 第二句", start_ms=1200, end_ms=2200),
    ])
    j1 = await make_organize_job(l1, v1)
    # V2 成为当前
    _, v2 = await add_transcript(l1, [
        _seg(0, "V2-ONLY-ANCHOR", start_ms=0, end_ms=1000),
        _seg(1, "V2 第二句", start_ms=1200, end_ms=2200),
    ])
    fake1 = FakeOrganizeModel()
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, j1, model=fake1)
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, j1)
        meeting = await db.get(Meeting, l1)
        notes = (await db.execute(
            select(NoteVersion).where(NoteVersion.lecture_id == l1)
        )).scalars().all()
        note_texts = "\n".join(
            (n.content or {}).get("title", "") + " " + json.dumps(n.content or {}, ensure_ascii=False)
            for n in notes
        )
    fixed_input = (
        j.status == "succeeded"
        and any(n.status == "draft" for n in notes)
        and "V1-ONLY-ANCHOR" in note_texts and "V2-ONLY-ANCHOR" not in note_texts
        and meeting.active_note_version_id is None
    )

    # (b) 损坏成功 Checkpoint → checkpoint_corrupt，不静默重建、不发起新调用
    l2, _ = await create_lecture(SUBJECT_A, "ca02-L2")
    _, v3 = await add_transcript(l2, [
        _seg(0, "损坏测试正文", start_ms=0, end_ms=1000),
    ])
    j2 = await make_organize_job(l2, v3)
    async with async_session_factory() as db:
        job = await db.get(ProcessingJob, j2)
        await organize_service.organize_service.build_manifest(db, job)  # 写入 manifest Checkpoint
        cp = await get_checkpoint(db, j2, "organize.manifest")
        cp.output_hash = "0" * 64  # 损坏摘要
        await db.commit()
    fake2 = FakeOrganizeModel()
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, j2, model=fake2)
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, j2)
    corrupt_ok = (
        j.status == "failed" and j.error_code == "checkpoint_corrupt"
        and fake2.calls == [] and fake2.summarize_calls == 0
    )

    # (c) 汇总 Checkpoint 复用前核对摘要：保持 hash 不变仅篡改 summary.title →
    #     checkpoint_corrupt 且不发布损坏产物（CA-02）
    l3, _ = await create_lecture(SUBJECT_A, "ca02-L3")
    _, v4 = await add_transcript(l3, [
        _seg(0, "汇总篡改测试正文", start_ms=0, end_ms=1000),
    ])
    j3 = await make_organize_job(l3, v4)
    organize_service._post_summary_hook = _boom_worker_crash
    try:
        async with async_session_factory() as db:
            await organize_service.organize_service.run_organize_job(db, j3, model=FakeOrganizeModel())
    except WorkerCrash:
        pass
    finally:
        organize_service._post_summary_hook = None
    async with async_session_factory() as db:
        sum_cp = await get_checkpoint(db, j3, "organize.summary")
        stored = dict(sum_cp.output_data or {})
        stored["title"] = "CORRUPTED-SUMMARY"  # 保持 output_hash 不变，仅改正文
        sum_cp.output_data = stored
        await db.commit()
    await expire_lease(j3)
    fake3 = FakeOrganizeModel()
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, j3, model=fake3)
    async with async_session_factory() as db:
        j3_row = await db.get(ProcessingJob, j3)
        meeting3 = await db.get(Meeting, l3)
        notes3 = (await db.execute(
            select(NoteVersion).where(NoteVersion.lecture_id == l3)
        )).scalars().all()
    summary_tamper_ok = (
        j3_row.status == "failed" and j3_row.error_code == "checkpoint_corrupt"
        and meeting3.active_note_version_id is None
        and all((n.content or {}).get("title") != "CORRUPTED-SUMMARY" for n in notes3)
    )

    # (d) 成功 extract 块 schema 身份篡改 → checkpoint_corrupt 不复用（CA-02 完整身份）
    l4, _ = await create_lecture(SUBJECT_A, "ca02-L4")
    _, v6 = await add_transcript(l4, make_long_segments(n=2, base=2000))
    j4 = await make_organize_job(l4, v6)
    fake4 = FakeOrganizeModel()
    fake4.crash_on_chunk = 1  # 块 0 提交后失联（块 1 处理前）
    crashed_d = False
    try:
        async with async_session_factory() as db:
            await organize_service.organize_service.run_organize_job(db, j4, model=fake4)
    except WorkerCrash:
        crashed_d = True
    async with async_session_factory() as db:
        cp0 = await get_checkpoint(db, j4, "organize.extract", "0")
        extract_committed = cp0 is not None and cp0.status == "succeeded"
        if extract_committed:
            cp0.schema_version = "incompatible-schema"  # 保持输出与 hash 不变，仅篡改 schema
            await db.commit()
    await expire_lease(j4)
    fake4b = FakeOrganizeModel()
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, j4, model=fake4b)
    async with async_session_factory() as db:
        j4_row = await db.get(ProcessingJob, j4)
    extract_tamper_ok = (
        crashed_d and extract_committed
        and j4_row.status == "failed" and j4_row.error_code == "checkpoint_corrupt"
        and fake4b.calls == [] and fake4b.summarize_calls == 0
    )

    record(
        "CA-02",
        "V2 成为当前后 V1 任务仍用 V1 + 损坏 Checkpoint 报 checkpoint_corrupt + 篡改 summary.title 不发布 + 成功块 schema 篡改不复用",
        fixed_input and corrupt_ok and summary_tamper_ok and extract_tamper_ok,
        dict(fixed_input=fixed_input, corrupt_ok=corrupt_ok,
             corrupt_status=j.status, corrupt_code=j.error_code,
             fake2_extract=len(fake2.calls), fake2_summarize=fake2.summarize_calls,
             summary_tamper_ok=summary_tamper_ok,
             tamper_status=j3_row.status, tamper_code=j3_row.error_code,
             extract_tamper_ok=extract_tamper_ok,
             extract_tamper_status=j4_row.status, extract_tamper_code=j4_row.error_code),
    )


# ── CA-03 分块恢复收益 ────────────────────────────────────────────────
async def ca_03(current):
    l1, _ = await create_lecture(SUBJECT_A, "ca03-L1")
    _, v1 = await add_transcript(l1, make_long_segments(n=20, base=2000))
    j1 = await make_organize_job(l1, v1)

    fake1 = FakeOrganizeModel()
    fake1.crash_on_chunk = 18  # K01~K18 提交后，处理 K19 时失联
    crashed = False
    try:
        async with async_session_factory() as db:
            await organize_service.organize_service.run_organize_job(db, j1, model=fake1)
    except WorkerCrash:
        crashed = True

    async with async_session_factory() as db:
        items = (await db.execute(
            select(ChunkItem).join(ChunkManifest, ChunkItem.manifest_id == ChunkManifest.id)
            .where(ChunkManifest.job_id == j1)
        )).scalars().all()
        done_before = sum(1 for it in items if it.status == "succeeded")
        extract_cps = (await db.execute(
            select(func.count(JobCheckpoint.id)).where(
                JobCheckpoint.job_id == j1, JobCheckpoint.step_key == "organize.extract",
                JobCheckpoint.status == "succeeded",
            )
        )).scalar_one()

    # 新进程恢复（租约过期后接管）
    await expire_lease(j1)
    fake2 = FakeOrganizeModel()
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, j1, model=fake2)

    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, j1)
        note = (await db.execute(
            select(NoteVersion).where(NoteVersion.lecture_id == l1)
        )).scalars().first()
        coverage = (note.content or {}).get("coverage", {}) if note else {}

    resumed_chunks = sorted(c["chunk_index"] for c in fake2.calls)
    resume_ok = (
        crashed and done_before == 18 and extract_cps == 18
        and len(fake2.calls) == 2 and resumed_chunks == [18, 19]
        and fake1.summarize_calls == 0  # 崩溃前未到汇总
        and fake2.summarize_calls == 1
        and j.status == "succeeded"
        and coverage.get("complete") is True and coverage.get("total_chunks") == 20
        and coverage.get("succeeded_chunks") == 20
    )

    record(
        "CA-03",
        "K01~K18 已提交块恢复不重复调用，仅处理 K19/K20，覆盖 20/20",
        resume_ok,
        dict(crashed=crashed, done_before=done_before, extract_cps=extract_cps,
             fake1_calls=len(fake1.calls), fake2_chunks=resumed_chunks,
             fake2_calls=len(fake2.calls), coverage=coverage),
    )


# ── CA-04 最终汇总恢复 ────────────────────────────────────────────────
async def ca_04(current):
    l1, _ = await create_lecture(SUBJECT_A, "ca04-L1")
    _, v1 = await add_transcript(l1, make_long_segments(n=2, base=200))
    j1 = await make_organize_job(l1, v1)

    fake1 = FakeOrganizeModel()
    organize_service._post_summary_hook = _boom_worker_crash  # 汇总 Checkpoint 提交后退出
    crashed = False
    try:
        async with async_session_factory() as db:
            await organize_service.organize_service.run_organize_job(db, j1, model=fake1)
    except WorkerCrash:
        crashed = True
    finally:
        organize_service._post_summary_hook = None

    async with async_session_factory() as db:
        sum_cp = await get_checkpoint(db, j1, "organize.summary")

    await expire_lease(j1)
    fake2 = FakeOrganizeModel()
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, j1, model=fake2)

    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, j1)
        meeting = await db.get(Meeting, l1)
        note = (await db.execute(
            select(NoteVersion).where(NoteVersion.lecture_id == l1)
        )).scalars().first()

    reuse_ok = (
        crashed and sum_cp is not None and sum_cp.status == "succeeded"
        and fake1.summarize_calls == 1 and fake2.summarize_calls == 0
        and j.status == "succeeded"
        and note is not None and note.status == "published"
        and meeting.active_note_version_id == note.id
    )

    record(
        "CA-04",
        "汇总 Checkpoint 提交后退出，恢复复用同一汇总结果，summarize 总调用 1 次",
        reuse_ok,
        dict(crashed=crashed, sum_cp=sum_cp.status if sum_cp else None,
             fake1_summarize=fake1.summarize_calls, fake2_summarize=fake2.summarize_calls,
             job_status=j.status, note_status=note.status if note else None),
    )


# ── CA-05 发布原子性与重投 ────────────────────────────────────────────
async def ca_05(current):
    # (a) 发布事务 commit 前中断 → 无半发布
    l1, _ = await create_lecture(SUBJECT_A, "ca05-L1")
    _, v1 = await add_transcript(l1, make_long_segments(n=2, base=200))
    j1 = await make_organize_job(l1, v1)
    organize_service._publish_hook = lambda: (_ for _ in ()).throw(RuntimeError("publish interrupted"))
    fake1 = FakeOrganizeModel()
    try:
        async with async_session_factory() as db:
            await organize_service.organize_service.run_organize_job(db, j1, model=fake1)
    finally:
        organize_service._publish_hook = None
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, j1)
        meeting = await db.get(Meeting, l1)
        note_count = (await db.execute(
            select(func.count(NoteVersion.id)).where(NoteVersion.lecture_id == l1)
        )).scalar_one()
        intent_count = (await db.execute(
            select(func.count(IndexIntent.id)).where(
                IndexIntent.note_version_id.in_(select(NoteVersion.id).where(NoteVersion.lecture_id == l1))
            )
        )).scalar_one()
    atomic_ok = (
        j.status == "failed" and meeting.active_note_version_id is None
        and note_count == 0 and intent_count == 0
    )

    # (b) 成功后重投同任务 → 不新增有效笔记/意向，不重调汇总
    l2, _ = await create_lecture(SUBJECT_A, "ca05-L2")
    _, v2 = await add_transcript(l2, make_long_segments(n=2, base=200))
    j2 = await make_organize_job(l2, v2)
    fake2 = FakeOrganizeModel()
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, j2, model=fake2)
    async with async_session_factory() as db:
        before = dict(
            note_count=(await db.execute(select(func.count(NoteVersion.id)).where(NoteVersion.lecture_id == l2))).scalar_one(),
            intent_count=(await db.execute(select(func.count(IndexIntent.id)).where(
                IndexIntent.note_version_id.in_(select(NoteVersion.id).where(NoteVersion.lecture_id == l2))
            ))).scalar_one(),
        )
    # 重投
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, j2, model=fake2)
    async with async_session_factory() as db:
        after = dict(
            note_count=(await db.execute(select(func.count(NoteVersion.id)).where(NoteVersion.lecture_id == l2))).scalar_one(),
            intent_count=(await db.execute(select(func.count(IndexIntent.id)).where(
                IndexIntent.note_version_id.in_(select(NoteVersion.id).where(NoteVersion.lecture_id == l2))
            ))).scalar_one(),
        )
    redeliver_ok = (
        before["note_count"] == 1 and after["note_count"] == 1
        and before["intent_count"] == 1 and after["intent_count"] == 1
        and fake2.summarize_calls == 1
    )

    record(
        "CA-05",
        "发布 commit 前无半发布 + 成功后重投不新增产物且不重调汇总",
        atomic_ok and redeliver_ok,
        dict(atomic_ok=atomic_ok, note_count=note_count, intent_count=intent_count,
             redeliver_ok=redeliver_ok, before=before, after=after,
             summarize_calls=fake2.summarize_calls),
    )


# ── CA-06 ASR 已知任务与结果恢复 ──────────────────────────────────────
async def ca_06(current):
    # (a) task_id 持久化后重启 → 不再 submit
    a1, _ = await create_lecture(SUBJECT_A, "ca06-a")
    jid_a = await create_asr_job(a1)
    fake_a1 = FakeASRProvider()
    job_service._submit_committed_hook = _boom_worker_lost
    crashed_a = False
    try:
        async with async_session_factory() as db:
            await job_service.run_job(db, jid_a, provider=fake_a1)
    except job_service.WorkerLost:
        crashed_a = True
    finally:
        job_service._submit_committed_hook = None
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, jid_a)
        task_id_saved = j.provider_task_id is not None
    await expire_lease(jid_a)  # 旧 worker 失联，租约过期后由新执行者接管
    fake_a2 = FakeASRProvider()
    async with async_session_factory() as db:
        await job_service.run_job(db, jid_a, provider=fake_a2)
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, jid_a)
        submit_cp = await get_checkpoint(db, jid_a, "asr.submit")
    no_resubmit = crashed_a and task_id_saved and fake_a1.submit_calls == 1 and fake_a2.submit_calls == 0
    atomic_publish = j.status == "succeeded" and j.output_version is not None and submit_cp.status == "succeeded"

    # (b) asr.result 保存后、解析/发布前重启 → 不再 poll
    b1, _ = await create_lecture(SUBJECT_A, "ca06-b")
    jid_b = await create_asr_job(b1)
    fake_b1 = FakeASRProvider()
    job_service._result_committed_hook = _boom_worker_lost
    crashed_b = False
    try:
        async with async_session_factory() as db:
            await job_service.run_job(db, jid_b, provider=fake_b1)
    except job_service.WorkerLost:
        crashed_b = True
    finally:
        job_service._result_committed_hook = None
    await expire_lease(jid_b)  # 旧 worker 失联，租约过期后由新执行者接管
    fake_b2 = FakeASRProvider()
    async with async_session_factory() as db:
        await job_service.run_job(db, jid_b, provider=fake_b2)
    async with async_session_factory() as db:
        jb = await db.get(ProcessingJob, jid_b)
        result_cp = await get_checkpoint(db, jid_b, "asr.result")
    no_repoll = crashed_b and fake_b1.poll_calls == 1 and fake_b2.poll_calls == 0
    result_atomic = jb.status == "succeeded" and jb.output_version is not None and result_cp.status == "succeeded"

    record(
        "CA-06",
        "task_id 持久化不重 submit + asr.result 持久化不重 poll + 版本/片段/Checkpoint 原子提交",
        no_resubmit and atomic_publish and no_repoll and result_atomic,
        dict(no_resubmit=no_resubmit, atomic_publish=atomic_publish,
             fake_a1_submit=fake_a1.submit_calls, fake_a2_submit=fake_a2.submit_calls,
             no_repoll=no_repoll, result_atomic=result_atomic,
             fake_b1_poll=fake_b1.poll_calls, fake_b2_poll=fake_b2.poll_calls),
    )


# ── CA-08 OSS 生命周期 ────────────────────────────────────────────────
async def ca_08(current):
    # (a) 提交后 object_key 持久化，成功终态后清理一次
    a1, _ = await create_lecture(SUBJECT_A, "ca08-a")
    jid_a = await create_asr_job(a1)
    fake_a = FakeASRProvider()
    async with async_session_factory() as db:
        await job_service.run_job(db, jid_a, provider=fake_a)
    async with async_session_factory() as db:
        submit_cp = await get_checkpoint(db, jid_a, "asr.submit")
        clean_cp = await get_checkpoint(db, jid_a, "asr.cleanup")
    object_key = (submit_cp.output_data or {}).get("object_key") if submit_cp else None
    lifecycle_ok = (
        fake_a.submit_calls == 1 and object_key in fake_a.submitted_object_keys
        and fake_a.cleanup_calls == 1 and clean_cp is not None and clean_cp.status == "succeeded"
    )

    # (b) 清理失败 → 可重试，且不重提 ASR
    b1, _ = await create_lecture(SUBJECT_A, "ca08-b")
    jid_b = await create_asr_job(b1)
    fake_b = FakeASRProvider()
    fake_b.cleanup_fail = True
    async with async_session_factory() as db:
        await job_service.run_job(db, jid_b, provider=fake_b)
    async with async_session_factory() as db:
        clean_b1 = await get_checkpoint(db, jid_b, "asr.cleanup")
    fake_b.cleanup_fail = False
    async with async_session_factory() as db:
        retried = await job_service.retry_source_cleanup(db, provider=fake_b)
    async with async_session_factory() as db:
        clean_b2 = await get_checkpoint(db, jid_b, "asr.cleanup")
    cleanup_retry_ok = (
        clean_b1 is not None and clean_b1.status == "failed"
        and clean_b2 is not None and clean_b2.status == "succeeded"
        and fake_b.submit_calls == 1 and retried >= 1
    )

    # (c) 轮询超时（retry_wait）不提前删源
    c1, _ = await create_lecture(SUBJECT_A, "ca08-c")
    jid_c = await create_asr_job(c1)
    fake_c = FakeASRProvider()
    fake_c.poll_result = "timeout"
    async with async_session_factory() as db:
        await job_service.run_job(db, jid_c, provider=fake_c)
    async with async_session_factory() as db:
        jc = await db.get(ProcessingJob, jid_c)
    no_early_delete = (
        jc.status == "retry_wait" and fake_c.cleanup_calls == 0 and fake_c.submit_calls == 1
    )

    record(
        "CA-08",
        "object_key 持久化 + 终态清理可重试不重提 + 超时不提前删源",
        lifecycle_ok and cleanup_retry_ok and no_early_delete,
        dict(lifecycle_ok=lifecycle_ok, object_key=object_key,
             cleanup_retry_ok=cleanup_retry_ok, retried=retried,
             no_early_delete=no_early_delete, fake_c_cleanup=fake_c.cleanup_calls),
    )


# ── CA-09 索引来源快照及块复用 ────────────────────────────────────────
async def ca_09(current):
    l1, c1 = await create_lecture(SUBJECT_A, "ca09-L1")
    segs_v1 = [
        _seg(i, f"U{i}-V1-" + "字" * 600, start_ms=i * 5000, end_ms=(i + 1) * 5000 - 100)
        for i in range(4)
    ]
    _, v1 = await add_transcript(l1, segs_v1)

    async with async_session_factory() as db:
        job1 = await index_service.index_service.create_index_job(
            db, ScopeContext(subject_id=SUBJECT_A), c1
        )
        jid1 = job1.id

    # 提交 2 单元后退出
    crash_embed = CrashEmbed(crash_after=2)
    crashed = False
    try:
        async with async_session_factory() as db:
            await index_service.index_service.run_index_job(db, jid1, embed=crash_embed)
    except index_service.BuildInterrupted:
        crashed = True

    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, jid1)
        iv = await db.get(IndexVersion, j.output_version)
        blocks_before = (await db.execute(
            select(IndexBlock).where(IndexBlock.index_version_id == iv.id)
        )).scalars().all()
        embed_cps_before = (await db.execute(
            select(func.count(JobCheckpoint.id)).where(
                JobCheckpoint.job_id == jid1, JobCheckpoint.step_key == "index.embed",
                JobCheckpoint.status == "succeeded",
            )
        )).scalar_one()
        manifest_cp = await get_checkpoint(db, jid1, "index.manifest")

    # 增改来源：V2 换正文并新增第 5 单元
    segs_v2 = [
        _seg(i, f"U{i}-V2-" + "字" * 600, start_ms=i * 5000, end_ms=(i + 1) * 5000 - 100)
        for i in range(5)
    ]
    _, v2 = await add_transcript(l1, segs_v2)

    # 恢复 job1：旧 worker 失联租约过期后接管，沿冻结清单续写，前 2 单元不再 embed
    await expire_lease(jid1)
    count_embed = CountingEmbed()
    async with async_session_factory() as db:
        await index_service.index_service.run_index_job(db, jid1, embed=count_embed)

    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, jid1)
        iv = await db.get(IndexVersion, j.output_version)
        blocks_after = (await db.execute(
            select(IndexBlock).where(IndexBlock.index_version_id == iv.id).order_by(IndexBlock.chunk_index)
        )).scalars().all()
    frozen_cont = (
        crashed and blocks_before and len(blocks_before) == 2 and embed_cps_before == 2
        and manifest_cp is not None and manifest_cp.status == "succeeded"
        and len(blocks_after) == 4 and count_embed.calls == 2
        and j.status == "succeeded"
        and all(("V1" in b.content) for b in blocks_after)  # 冻结清单正文，未误用 V2
    )

    # 显式新建构建：来源变化后 create_index_job 应建立独立任务/快照（CA-09），
    # 再经 run_index_job 全量处理新来源（5 单元 V2），不能复用旧 succeeded 任务。
    async with async_session_factory() as db:
        job2 = await index_service.index_service.create_index_job(
            db, ScopeContext(subject_id=SUBJECT_A), c1
        )
        jid2 = job2.id
    count_embed2 = CountingEmbed()
    async with async_session_factory() as db:
        await index_service.index_service.run_index_job(db, jid2, embed=count_embed2)
    async with async_session_factory() as db:
        j2 = await db.get(ProcessingJob, jid2)
        iv_new = await db.get(IndexVersion, j2.output_version)
        new_blocks = (await db.execute(
            select(IndexBlock).where(IndexBlock.index_version_id == iv_new.id)
        )).scalars().all()
    independent_snapshot = (
        jid2 != jid1
        and j2.status == "succeeded"
        and len(new_blocks) == 5 and count_embed2.calls == 5
        and all(("V2" in b.content) for b in new_blocks)
    )

    record(
        "CA-09",
        "恢复沿冻结清单续写前2单元不重embed + 新来源经 create_index_job 建立独立快照全量处理",
        frozen_cont and independent_snapshot,
        dict(crashed=crashed, blocks_before=len(blocks_before), embed_cps_before=embed_cps_before,
             blocks_after=len(blocks_after), count_embed_calls=count_embed.calls,
             frozen_cont=frozen_cont, jid2_new=(jid2 != jid1), new_blocks=len(new_blocks),
             count_embed2_calls=count_embed2.calls, independent_snapshot=independent_snapshot),
    )


# ── CA-11 领取、续租和旧执行者隔离 ────────────────────────────────────
async def ca_11(current):
    l1, _ = await create_lecture(SUBJECT_A, "ca11-L1")
    _, v1 = await add_transcript(l1, make_long_segments(n=2, base=200))
    jid = await make_organize_job(l1, v1)

    owner1 = lease_service.new_owner()
    owner2 = lease_service.new_owner()

    # 两 worker 并发竞争 → 仅一个有效 owner
    async def _claim(owner):
        async with async_session_factory() as db:
            claimed, epoch = await lease_service.claim_job(db, jid, owner)
            await db.commit()
            return claimed, epoch

    r1, r2 = await asyncio.gather(_claim(owner1), _claim(owner2))
    wins = [r for r in (r1, r2) if r[0]]
    one_owner = len(wins) == 1

    # 确定赢家与败者
    if r1[0]:
        winner_owner, winner_epoch = owner1, r1[1]
        loser_owner = owner2
    else:
        winner_owner, winner_epoch = owner2, r2[1]
        loser_owner = owner1

    # 续租（健康长调用心跳），败者仍不能接管
    async with async_session_factory() as db:
        renewed = await lease_service.renew_lease(db, jid, winner_owner, winner_epoch)
        await db.commit()
    async with async_session_factory() as db:
        assert_owner_ok = await lease_service.assert_owner(db, jid, winner_owner, winner_epoch)
    async def _claim_loser():
        async with async_session_factory() as db:
            claimed, epoch = await lease_service.claim_job(db, jid, loser_owner)
            await db.commit()
            return claimed
    loser_blocked = not await _claim_loser()

    # 断开原 worker（租约过期）→ 接管成功，epoch 递增
    await expire_lease(jid)
    async with async_session_factory() as db:
        claimed2, epoch2 = await lease_service.claim_job(db, jid, owner2)
        await db.commit()
    taken_over = claimed2 and epoch2 > winner_epoch

    # 旧执行者（旧 epoch）不能再提交 / 新调用
    async with async_session_factory() as db:
        old_assert = await lease_service.assert_owner(db, jid, winner_owner, winner_epoch)
        old_assert_locked = await lease_service.assert_owner(
            db, jid, winner_owner, winner_epoch, for_update=True
        )
        await db.rollback()  # 释放 FOR UPDATE 行锁
    stale_rejected = (not old_assert) and (not old_assert_locked)

    # 旧执行者（旧 epoch）不能续租：原子条件 UPDATE 命中 0 行（CA-11 过期 owner 不能 renew）
    async with async_session_factory() as db:
        old_renew = await lease_service.renew_lease(db, jid, winner_owner, winner_epoch)
        await db.commit()
    stale_renew_rejected = not old_renew

    # 中间产物提交隔离（CA-11）：extract 外发期间被接管 → 旧执行者不得提交 extract 成功块
    l2, _ = await create_lecture(SUBJECT_A, "ca11-L2")
    _, v2 = await add_transcript(l2, make_long_segments(n=2, base=2000))
    jid2 = await make_organize_job(l2, v2)
    replacement_owner = lease_service.new_owner()
    takeover_model = TakeoverExtractModel(jid2, replacement_owner)
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, jid2, model=takeover_model)
    async with async_session_factory() as db:
        j2 = await db.get(ProcessingJob, jid2)
        extract_cps = (await db.execute(
            select(func.count(JobCheckpoint.id)).where(
                JobCheckpoint.job_id == jid2, JobCheckpoint.step_key == "organize.extract",
                JobCheckpoint.status == "succeeded",
            )
        )).scalar_one()
    extract_isolated = (
        takeover_model.done and extract_cps == 0 and j2.lease_owner == replacement_owner
    )

    # 中间产物提交隔离（CA-11）：embed 外发期间被接管 → 旧执行者不得再次 embed / 提交单元
    l3, c3 = await create_lecture(SUBJECT_A, "ca11-L3")
    _, v3 = await add_transcript(l3, make_long_segments(n=2, base=2000))
    async with async_session_factory() as db:
        jid3 = (await index_service.index_service.create_index_job(
            db, ScopeContext(subject_id=SUBJECT_A), c3
        )).id
    replacement2 = lease_service.new_owner()
    takeover_embed = TakeoverEmbed(jid3, replacement2)
    async with async_session_factory() as db:
        await index_service.index_service.run_index_job(db, jid3, embed=takeover_embed)
    async with async_session_factory() as db:
        j3 = await db.get(ProcessingJob, jid3)
        embed_cps = (await db.execute(
            select(func.count(JobCheckpoint.id)).where(
                JobCheckpoint.job_id == jid3, JobCheckpoint.step_key == "index.embed",
                JobCheckpoint.status == "succeeded",
            )
        )).scalar_one()
    index_isolated = (
        takeover_embed.done and takeover_embed.calls == 1
        and embed_cps == 0 and j3.lease_owner == replacement2
    )

    # ASR 独立续租（CA-11）：poll 超过租约时长仍健康完成（独立 session 心跳续租）
    a_hb, _ = await create_lecture(SUBJECT_A, "ca11-asr-hb")
    jid_hb = await create_asr_job(a_hb)
    slow_asr = SlowASRProvider(delay=1.5)
    orig_lease = settings.CHECKPOINT_LEASE_SECONDS
    orig_hb = settings.CHECKPOINT_HEARTBEAT_SECONDS
    settings.CHECKPOINT_LEASE_SECONDS = 1
    settings.CHECKPOINT_HEARTBEAT_SECONDS = 0.2
    try:
        async with async_session_factory() as db:
            await job_service.run_job(db, jid_hb, provider=slow_asr)
    finally:
        settings.CHECKPOINT_LEASE_SECONDS = orig_lease
        settings.CHECKPOINT_HEARTBEAT_SECONDS = orig_hb
    async with async_session_factory() as db:
        j_hb = await db.get(ProcessingJob, jid_hb)
    asr_hb_ok = (
        slow_asr.poll_calls == 1 and j_hb.status == "succeeded" and j_hb.output_version is not None
    )

    # index 独立续租（CA-11）：逐块向量化超过租约时长仍健康完成
    l4, c4 = await create_lecture(SUBJECT_A, "ca11-index-hb")
    _, v4 = await add_transcript(l4, make_long_segments(n=4, base=2000))
    async with async_session_factory() as db:
        jid_hb2 = (await index_service.index_service.create_index_job(
            db, ScopeContext(subject_id=SUBJECT_A), c4
        )).id
    slow_embed = SlowEmbed(delay=0.5)
    settings.CHECKPOINT_LEASE_SECONDS = 1
    settings.CHECKPOINT_HEARTBEAT_SECONDS = 0.2
    try:
        async with async_session_factory() as db:
            await index_service.index_service.run_index_job(db, jid_hb2, embed=slow_embed)
    finally:
        settings.CHECKPOINT_LEASE_SECONDS = orig_lease
        settings.CHECKPOINT_HEARTBEAT_SECONDS = orig_hb
    async with async_session_factory() as db:
        j_hb2 = await db.get(ProcessingJob, jid_hb2)
    index_hb_ok = (
        slow_embed.calls == 4 and j_hb2.status == "succeeded" and j_hb2.output_version is not None
    )

    # 迟到异常隔离（CA-11）：ASR poll 外发期间被接管后返回可重试 ProviderError →
    # 旧执行者不得把新 owner 改写为 retry_wait/provider_error，也不建立重试计划。
    a_err, _ = await create_lecture(SUBJECT_A, "ca11-asr-err")
    jid_err = await create_asr_job(a_err)
    repl_err = lease_service.new_owner()
    err_asr = TakeoverErrorASRProvider(jid_err, repl_err)
    async with async_session_factory() as db:
        await job_service.run_job(db, jid_err, provider=err_asr)
    async with async_session_factory() as db:
        j_err = await db.get(ProcessingJob, jid_err)
    asr_err_isolated = (
        err_asr.done
        and j_err.lease_owner == repl_err
        and j_err.status == "running"
        and j_err.error_type is None
        and j_err.retry_at is None
    )

    # 迟到异常隔离（CA-11）：organize summarize 外发期间被接管后返回可重试 ModelError →
    # 旧执行者不得把新 owner 改写为 failed/model_error。
    l5, _ = await create_lecture(SUBJECT_A, "ca11-org-err")
    _, v5 = await add_transcript(l5, make_long_segments(n=2, base=2000))
    jid_err2 = await make_organize_job(l5, v5)
    repl_err2 = lease_service.new_owner()
    err_model = TakeoverErrorModel(jid_err2, repl_err2)
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, jid_err2, model=err_model)
    async with async_session_factory() as db:
        j_err2 = await db.get(ProcessingJob, jid_err2)
    org_err_isolated = (
        err_model.done
        and j_err2.lease_owner == repl_err2
        and j_err2.status == "running"
        and j_err2.error_type is None
    )

    record(
        "CA-11",
        "并发仅1 owner + 健康续租不被误接管 + 过期 epoch 拒绝写入/续租 + 中间产物提交隔离 + ASR/索引独立续租 + 迟到异常不改写新 owner",
        one_owner and renewed and assert_owner_ok and loser_blocked and taken_over
        and stale_rejected and stale_renew_rejected
        and extract_isolated and index_isolated and asr_hb_ok and index_hb_ok
        and asr_err_isolated and org_err_isolated,
        dict(one_owner=one_owner, r1_claimed=r1[0], r2_claimed=r2[0],
             renewed=renewed, assert_owner_ok=assert_owner_ok, loser_blocked=loser_blocked,
             winner_epoch=winner_epoch, epoch2=epoch2, taken_over=taken_over,
             stale_rejected=stale_rejected, stale_renew_rejected=stale_renew_rejected,
             extract_isolated=extract_isolated, extract_cps=extract_cps,
             index_isolated=index_isolated, takeover_embed_calls=takeover_embed.calls,
             embed_cps=embed_cps, asr_hb_ok=asr_hb_ok,
             asr_poll_calls=slow_asr.poll_calls, index_hb_ok=index_hb_ok,
             index_embed_calls=slow_embed.calls,
             asr_err_isolated=asr_err_isolated,
             asr_err_owner=j_err.lease_owner, asr_err_status=j_err.status,
             asr_err_type=j_err.error_type, asr_err_retry_at=j_err.retry_at,
             org_err_isolated=org_err_isolated,
             org_err_owner=j_err2.lease_owner, org_err_status=j_err2.status,
             org_err_type=j_err2.error_type),
    )


# ── S2 助手：真实 Redis 令牌桶 / 入口保护 / 故障注入 ─────────────────

_FASTAPI_APP = None


def _asgi_transport():
    global _FASTAPI_APP
    from httpx import ASGITransport
    if _FASTAPI_APP is None:
        from app.main import app as fastapi_app  # noqa: PLC0415
        _FASTAPI_APP = fastapi_app
    return ASGITransport(app=_FASTAPI_APP)


def _make_wav_bytes(duration_seconds: float = 1.0, rate: int = 8000) -> bytes:
    """生成最小有效 PCM WAV（静音），供音频入口 HTTP 请求（≥500ms）。"""
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * duration_seconds))
    return buf.getvalue()


async def _flush_keys(*keys) -> None:
    pool = await rl._get_pool()
    if keys:
        await pool.delete(*keys)
    else:
        await pool.flushdb()


async def _point_redis(url: str) -> None:
    """切换 REDIS_URL 并关闭连接池，使下一次 consume 用新地址重建（CA-16 断连/恢复）。"""
    settings.REDIS_URL = url
    os.environ["REDIS_URL"] = url
    await rl.close_pool()


def _run_worker(key: str, *, capacity: float, refill: float, count: int, cost: float = 1.0) -> int:
    """启动独立进程 worker 消费给定桶，返回放行次数（CA-13 两进程共享桶）。"""
    env = dict(os.environ)
    env.update(
        REDIS_URL=settings.REDIS_URL, PYTHONPATH=".", PYTHONUTF8="1",
        RATE_LIMIT_ENABLED="true", OPENAI_API_KEY="", DASHSCOPE_API_KEY="",
    )
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "s2_redis_worker.py"),
         "--key", key, "--capacity", str(capacity), "--refill", str(refill),
         "--count", str(count), "--cost", str(cost)],
        env=env, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    if r.returncode != 0:
        raise RuntimeError(f"s2_redis_worker failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
    return int(r.stdout.strip())


async def _exhaust_user_bucket(family: str, capacity: int = 3, refill: float = 0.1) -> None:
    key = rl.user_key(settings.LOCAL_SUBJECT_ID, family)
    await _flush_keys(key)
    for _ in range(capacity):
        await rl.consume(key, capacity, refill, 1)


async def _count_scalar(expr) -> int:
    async with async_session_factory() as db:
        return (await db.execute(select(func.count()).select_from(expr))).scalar_one()


async def _job_outbox_counts(type_: str, resource_id=None) -> dict:
    async with async_session_factory() as db:
        job_q = select(func.count(ProcessingJob.id)).where(ProcessingJob.type == type_)
        outbox_q = select(func.count(OutboxEvent.id))
        if resource_id is not None:
            job_q = job_q.where(ProcessingJob.resource_id == resource_id)
            outbox_q = outbox_q.where(
                OutboxEvent.job_id.in_(select(ProcessingJob.id).where(ProcessingJob.resource_id == resource_id))
            )
        return {
            "jobs": (await db.execute(job_q)).scalar_one(),
            "outbox": (await db.execute(outbox_q)).scalar_one(),
        }


# ── CA-12 原子令牌桶与补充 ────────────────────────────────────────────
async def ca_12(current):
    from redis.exceptions import ResponseError

    # (a) 同一 Redis 窗口 50 并发竞争 capacity=3 桶 → 仅 3 次放行（零补充边界）
    k_burst = rl.user_key("ca12-burst", "burst")
    await _flush_keys(k_burst)
    burst = await asyncio.gather(*[rl.consume(k_burst, 3, 0.1, 1) for _ in range(50)])
    burst_allowed = sum(1 for r in burst if r.allowed)
    burst_remaining = min(r.remaining for r in burst if not r.allowed)
    burst_ok = burst_allowed == 3 and burst_remaining >= 0

    # (b) 推进时间验证补充/上限（capacity=2, refill=1/s 干净算例）
    k_refill = rl.user_key("ca12-refill", "refill")
    await _flush_keys(k_refill)
    r1 = await rl.consume(k_refill, 2, 1, 1)
    r2 = await rl.consume(k_refill, 2, 1, 1)
    r_partial = await rl.consume(k_refill, 2, 1, 1)  # 立即：拒绝
    await asyncio.sleep(1.5)  # 补充 1.5 → 截断为容量 2
    r_cap = await rl.consume(k_refill, 2, 1, 1)  # 放行，remaining≈0.5
    refill_ok = (
        r1.allowed and r2.allowed and not r_partial.allowed
        and r_cap.allowed and 0.4 < r_cap.remaining < 1.5
    )
    # 上限：再等足够久也不会超过容量（无超容量）
    await _flush_keys(k_refill)
    await rl.consume(k_refill, 2, 1, 1)
    await rl.consume(k_refill, 2, 1, 1)
    await asyncio.sleep(5)
    c1 = await rl.consume(k_refill, 2, 1, 1)
    c2 = await rl.consume(k_refill, 2, 1, 1)
    c3 = await rl.consume(k_refill, 2, 1, 1)
    cap_ok = c1.allowed and c2.allowed and not c3.allowed

    # (c) 非法配置拒绝：应用侧 ValueError + Lua 侧 error_reply
    bad_app = 0
    for bad in ((0, 1, 1), (3, 1, 4)):  # capacity<=0；cost>capacity
        try:
            await rl.consume(k_refill, *bad)
        except ValueError:
            bad_app += 1
    pool = await rl._get_pool()
    sha = await rl._ensure_script(pool)
    bad_lua = False
    try:
        await pool.evalsha(sha, 1, k_refill, "0", "1", "1")
    except ResponseError:
        bad_lua = True
    bad_ok = bad_app == 2 and bad_lua

    # (d) 时钟回退不多补：把 last_ms 拨到未来 → elapsed 归零，不多补令牌
    k_rollback = rl.user_key("ca12-rollback", "rollback")
    await _flush_keys(k_rollback)
    for _ in range(3):
        await rl.consume(k_rollback, 3, 1, 1)  # 空桶
    t = await pool.time()
    future_ms = int(t[0]) * 1000 + int(t[1]) // 1000 + 3600_000
    await pool.hset(k_rollback, mapping={"tokens": "0", "last_ms": str(future_ms)})
    rb = await rl.consume(k_rollback, 3, 1, 1)
    rollback_ok = (not rb.allowed) and rb.remaining == 0

    # (e) TTL 公式：capacity=100, refill=1 → ceil(100/1*2000)=200000ms（>60000 下限）
    k_ttl = rl.user_key("ca12-ttl", "ttl")
    await _flush_keys(k_ttl)
    await rl.consume(k_ttl, 100, 1, 1)
    pttl = await pool.pttl(k_ttl)
    ttl_ok = 190000 <= pttl <= 200000

    record(
        "CA-12",
        "并发50仅3放行 + 按时间补充/上限不超容量 + 坏参数拒绝 + 时钟回退不多补 + TTL 公式",
        burst_ok and refill_ok and cap_ok and bad_ok and rollback_ok and ttl_ok,
        dict(burst_allowed=burst_allowed, burst_remaining=burst_remaining, burst_ok=burst_ok,
             refill_ok=refill_ok, r_cap_remaining=r_cap.remaining,
             cap_ok=cap_ok, bad_app=bad_app, bad_lua=bad_lua,
             rollback_ok=rollback_ok, rb_remaining=rb.remaining,
             ttl_ok=ttl_ok, pttl=pttl),
    )


# ── CA-13 两进程共享与身份隔离 ────────────────────────────────────────
async def ca_13(current):
    # (a) 两进程共享桶：父进程消费 1，子进程（独立进程）消费 3 → 仅放行 2；B 独立满桶 3
    key_a = rl.user_key("subject-a", "ca13")
    key_b = rl.user_key("subject-b", "ca13")
    await _flush_keys(key_a, key_b)
    await rl.consume(key_a, 3, 0.1, 1)  # 父进程占 1 → 剩 2
    child_a = _run_worker(key_a, capacity=3, refill=0.1, count=3)
    child_b = _run_worker(key_b, capacity=3, refill=0.1, count=3)
    shared_ok = child_a == 2 and child_b == 3

    # (b) 身份注入边界：服务端主体来自 local_scope()，客户端伪造 subject_id 不改桶
    spoof_local = rl.user_key(settings.LOCAL_SUBJECT_ID, rl.FAMILY_AUDIO_UPLOAD)
    spoof_b = rl.user_key("subject-b", rl.FAMILY_AUDIO_UPLOAD)
    await _flush_keys(spoof_local, spoof_b)
    await _exhaust_user_bucket(rl.FAMILY_AUDIO_UPLOAD)
    l_ok, _ = await create_lecture(settings.LOCAL_SUBJECT_ID, "ca13-spoof")
    transport = _asgi_transport()
    from httpx import AsyncClient
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/lectures/{l_ok}/audio",
            files={"file": ("a.wav", _make_wav_bytes(), "audio/wav")},
            params={"subject_id": "subject-b"},  # 客户端伪造 subject_id
        )
    spoof_rejected = resp.status_code == 429
    b_check = await rl.consume(spoof_b, 3, 0.1, 1)
    b_untouched = b_check.allowed and b_check.remaining == 2
    identity_ok = spoof_rejected and b_untouched

    # (c) 共享配额组不因模型名拆分绕过：gen 桶按 credential_alias（name），换 model 名不换桶
    from app.services.organize_model import RealOrganizeModel
    m1 = RealOrganizeModel({"model": "qwen-plus"})
    m2 = RealOrganizeModel({"model": "qwen-max"})
    alias_key = rl.provider_key(m1.name, rl.FAMILY_GEN)
    alias_key2 = rl.provider_key(m2.name, rl.FAMILY_GEN)
    await _flush_keys(alias_key)
    ok1 = await rl.consume(alias_key, settings.RATE_PROVIDER_GEN_CAPACITY, settings.RATE_PROVIDER_GEN_REFILL, 1)
    ok2 = await rl.consume(alias_key, settings.RATE_PROVIDER_GEN_CAPACITY, settings.RATE_PROVIDER_GEN_REFILL, 1)
    alias_rejected = False
    try:
        await rl.consume_provider_bucket(rl.FAMILY_GEN, m2.name)
    except rl.RateLimitedError:
        alias_rejected = True
    model_alias_ok = (
        m1.name == m2.name == "real" and alias_key == alias_key2
        and ok1.allowed and ok2.allowed and alias_rejected
    )

    record(
        "CA-13",
        "A 跨进程共享额度/B 独立 + 客户端伪造 subject_id 不改桶 + 共享配额组不因模型名拆分绕过",
        shared_ok and identity_ok and model_alias_ok,
        dict(child_a=child_a, child_b=child_b, shared_ok=shared_ok,
             spoof_rejected=spoof_rejected, b_untouched=b_untouched,
             alias_key=alias_key, alias_key2=alias_key2,
             m1_name=m1.name, m2_name=m2.name, model_alias_ok=model_alias_ok),
    )


# ── CA-14 入口覆盖与拒绝副作用 ────────────────────────────────────────
async def _entry_429(client, family, before_fn, after_fn, request_fn) -> dict:
    """耗尽指定用户桶后走一个入口，断言 429 + Retry-After + 无副作用。"""
    await _exhaust_user_bucket(family)
    before = await before_fn()
    resp = await request_fn()
    after = await after_fn()
    body = resp.json()
    return {
        "status": resp.status_code,
        "code": body.get("code"),
        "retry_after_seconds": body.get("retry_after_seconds"),
        "retry_after_header": resp.headers.get("Retry-After"),
        "no_delta": before == after,
        "before": before, "after": after,
    }


async def ca_14(current):
    l_ok, c_ok = await create_lecture(settings.LOCAL_SUBJECT_ID, "ca14-ok")
    l_forbid, c_forbid = await create_lecture(SUBJECT_B, "ca14-forbid")
    _, v_ok = await add_transcript(l_ok, make_long_segments(n=2, base=200))
    retry_jid = await create_asr_job(l_ok, subject=settings.LOCAL_SUBJECT_ID)
    async with async_session_factory() as db:
        rj = await db.get(ProcessingJob, retry_jid)
        rj.status = "failed"
        await db.commit()

    transport = _asgi_transport()
    from httpx import AsyncClient

    async def _asset_count():
        async with async_session_factory() as db:
            return (await db.execute(select(func.count(AudioAsset.id)))).scalar_one()

    async def _doc_count():
        async with async_session_factory() as db:
            return {
                "docs": (await db.execute(select(func.count(SourceDocument.id)))).scalar_one(),
                "versions": (await db.execute(select(func.count(SourceDocumentVersion.id)))).scalar_one(),
            }

    async def _retry_state():
        async with async_session_factory() as db:
            j = await db.get(ProcessingJob, retry_jid)
            return {"status": j.status, "attempt": j.attempt, "error_code": j.error_code}

    entries = {}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 读状态可用（不消耗桶）
        r_get = await client.get(f"/api/lectures/{l_ok}")
        entries["read_status"] = r_get.status_code == 200

        # 越权 404 且无副作用（访问检查先于桶检查）
        before_f = await _asset_count()
        r_forbid = await client.get(f"/api/lectures/{l_forbid}")
        after_f = await _asset_count()
        entries["cross_tenant_404"] = r_forbid.status_code == 404 and before_f == after_f

        # 音频上传
        entries["audio"] = await _entry_429(
            client, rl.FAMILY_AUDIO_UPLOAD,
            before_fn=_asset_count, after_fn=_asset_count,
            request_fn=lambda: client.post(
                f"/api/lectures/{l_ok}/audio",
                files={"file": ("a.wav", _make_wav_bytes(), "audio/wav")},
            ),
        )
        # 整理
        entries["organize"] = await _entry_429(
            client, rl.FAMILY_ORGANIZE,
            before_fn=lambda: _job_outbox_counts("organize", l_ok),
            after_fn=lambda: _job_outbox_counts("organize", l_ok),
            request_fn=lambda: client.post(
                f"/api/lectures/{l_ok}/organize",
                json={"transcript_version_id": str(v_ok)},
            ),
        )
        # 文档引起索引
        entries["document"] = await _entry_429(
            client, rl.FAMILY_DOCUMENT_INDEX,
            before_fn=_doc_count, after_fn=_doc_count,
            request_fn=lambda: client.post(
                f"/api/courses/{c_ok}/documents",
                files={"file": ("doc.txt", "课件正文".encode("utf-8"), "text/plain")},
            ),
        )
        # 显式索引
        entries["explicit_index"] = await _entry_429(
            client, rl.FAMILY_EXPLICIT_INDEX,
            before_fn=lambda: _job_outbox_counts("index", c_ok),
            after_fn=lambda: _job_outbox_counts("index", c_ok),
            request_fn=lambda: client.post(f"/api/courses/{c_ok}/index"),
        )
        # 手动重试
        entries["retry"] = await _entry_429(
            client, rl.FAMILY_RETRY,
            before_fn=_retry_state, after_fn=_retry_state,
            request_fn=lambda: client.post(f"/api/jobs/{retry_jid}/retry"),
        )

        # 读状态 / 取消在桶耗尽后仍可用（不消耗桶）
        r_job = await client.get(f"/api/jobs/{retry_jid}")
        r_cancel = await client.post(f"/api/jobs/{retry_jid}/cancel")
        entries["read_cancel_usable"] = (
            r_job.status_code == 200 and r_cancel.status_code == 200
        )

    # 逐入口断言：429 + code=rate_limited + Retry-After 头 + 无副作用
    protected = ["audio", "organize", "document", "explicit_index", "retry"]
    entry_ok = all(
        entries[k]["status"] == 429
        and entries[k]["code"] == "rate_limited"
        and entries[k]["retry_after_header"] is not None
        and entries[k]["retry_after_seconds"] is not None
        and 9 <= entries[k]["retry_after_seconds"] <= 10
        and str(entries[k]["retry_after_seconds"]) == entries[k]["retry_after_header"]
        and entries[k]["no_delta"]
        for k in protected
    )

    record(
        "CA-14",
        "五个受控入口 429/Retry-After 无新产物 + 读/取消可用 + 越权 404 无副作用",
        entry_ok and entries["read_status"] and entries["read_cancel_usable"] and entries["cross_tenant_404"],
        dict(entry_ok=entry_ok, read_status=entries["read_status"],
             read_cancel_usable=entries["read_cancel_usable"],
             cross_tenant_404=entries["cross_tenant_404"],
             audio=entries["audio"], organize=entries["organize"],
             document=entries["document"], explicit_index=entries["explicit_index"],
             retry=entries["retry"]),
    )


# ── CA-16 Redis 不可用及脚本缓存故障 ──────────────────────────────────
async def ca_16(current):
    real_url = settings.REDIS_URL

    # (a) NOSCRIPT 安全加载：清脚本缓存后 consume 自动重载一次
    pool = await rl._get_pool()
    await pool.script_flush()
    k_ns = rl.user_key("ca16-noscript", rl.FAMILY_ORGANIZE)
    await _flush_keys(k_ns)
    r_ns = await rl.consume(k_ns, 3, 0.1, 1)
    noscript_ok = r_ns.allowed

    # (b) 付费入口 503 + 读/取消仍可用（Redis 断连）
    l_ok, _ = await create_lecture(settings.LOCAL_SUBJECT_ID, "ca16-entry")
    _, v_ok = await add_transcript(l_ok, make_long_segments(n=2, base=200))
    jid = await create_asr_job(l_ok, subject=settings.LOCAL_SUBJECT_ID)
    await _point_redis(DEAD_REDIS_URL)
    transport = _asgi_transport()
    from httpx import AsyncClient
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r_org = await client.post(
            f"/api/lectures/{l_ok}/organize", json={"transcript_version_id": str(v_ok)}
        )
        r_get = await client.get(f"/api/jobs/{jid}")
        r_cancel = await client.post(f"/api/jobs/{jid}/cancel")
    entry_503 = r_org.status_code == 503 and r_org.json().get("code") == "rate_limit_unavailable"
    read_cancel_ok = r_get.status_code == 200 and r_cancel.status_code == 200
    await _point_redis(real_url)

    # (c) worker 未出网：Redis 断连 → run_job → retry_wait + rate_limit_unavailable + 无 submit/poll
    jid2 = await create_asr_job(l_ok, subject=settings.LOCAL_SUBJECT_ID)
    fake2 = FakeASRProvider()
    await _point_redis(DEAD_REDIS_URL)
    async with async_session_factory() as db:
        await job_service.run_job(db, jid2, provider=fake2)
    async with async_session_factory() as db:
        j2 = await db.get(ProcessingJob, jid2)
    worker_wait_ok = (
        j2.status == "retry_wait"
        and j2.wait_reason == "rate_limit_unavailable"
        and j2.retry_at is not None
        and j2.retry_at >= now() + timedelta(seconds=4)
        and fake2.submit_calls == 0 and fake2.poll_calls == 0
    )
    await _point_redis(real_url)

    # (d) 恢复不丢 Checkpoint：成功 ASR 落 Checkpoint 后 Redis 重启（清桶+脚本缓存），
    #     PostgreSQL 中的 Checkpoint 不受影响。
    jid3 = await create_asr_job(l_ok, subject=settings.LOCAL_SUBJECT_ID)
    fake3 = FakeASRProvider()
    async with async_session_factory() as db:
        await job_service.run_job(db, jid3, provider=fake3)
    async with async_session_factory() as db:
        j3 = await db.get(ProcessingJob, jid3)
        cp_before = (await db.execute(
            select(func.count(JobCheckpoint.id)).where(JobCheckpoint.job_id == jid3)
        )).scalar_one()
        succeeded = j3.status == "succeeded"
    await pool.flushdb()
    await pool.script_flush()
    async with async_session_factory() as db:
        cp_after = (await db.execute(
            select(func.count(JobCheckpoint.id)).where(JobCheckpoint.job_id == jid3)
        )).scalar_one()
    recover_cp_ok = succeeded and cp_before >= 1 and cp_before == cp_after

    record(
        "CA-16",
        "NOSCRIPT 安全重载 + 入口 503/读取消可用 + worker 持久等待未出网 + 恢复不丢 Checkpoint",
        noscript_ok and entry_503 and read_cancel_ok and worker_wait_ok and recover_cp_ok,
        dict(noscript_ok=noscript_ok, entry_503=entry_503, read_cancel_ok=read_cancel_ok,
             worker_wait_ok=worker_wait_ok, j2_status=j2.status, j2_wait_reason=j2.wait_reason,
             j2_retry_at=j2.retry_at, fake2_submit=fake2.submit_calls, fake2_poll=fake2.poll_calls,
             recover_cp_ok=recover_cp_ok, cp_before=cp_before, cp_after=cp_after),
    )


# ── CA-07 外部未知窗口 ────────────────────────────────────────────────
async def ca_07(current):
    await _s3_reset()

    # (a) ASR 提交未知：1 次 transport，provider_calls 为 unknown，预留保留
    a, _ = await create_lecture(SUBJECT_A, "ca07-asr")
    jid = await create_asr_job(a)
    p = FakeASRProvider()
    p.submit_unknown = True
    async with async_session_factory() as db:
        await job_service.run_job(db, jid, provider=p)
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, jid)
    rows = await _pc_rows(f"asr:submit:{jid}")
    asr_ok = (
        j.status == "failed" and j.error_type == "provider_unknown"
        and p.submit_calls == 1 and len(rows) == 1 and rows[0]["status"] == "unknown"
        and rows[0]["usage_state"] == "unknown"
        and rows[0]["settled_amount"] == rows[0]["reserved_amount"] > 0
    )

    # (b) organize 汇总未知：extract 成功后 summarize 未知 → 阻断普通重试
    await _s3_reset()
    l2, _ = await create_lecture(SUBJECT_A, "ca07-org")
    _, v2 = await add_transcript(l2, make_long_segments(n=1, base=200))
    jid2 = await make_organize_job(l2, v2)
    m = FakeOrganizeModel()
    m.summarize_unknown = True
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, jid2, model=m)
    async with async_session_factory() as db:
        j2 = await db.get(ProcessingJob, jid2)
    srows = await _pc_rows(f"organize:summarize:{jid2}")
    org_ok = (
        j2.status == "failed" and j2.error_type == "provider_unknown"
        and len(srows) == 1 and srows[0]["status"] == "unknown"
        and srows[0]["settled_amount"] == srows[0]["reserved_amount"] > 0
    )

    # (c) embedding 未知：首次向量化即未知 → 阻断，仅 1 次 transport
    await _s3_reset()
    l3, c3 = await create_lecture(SUBJECT_A, "ca07-emb")
    await add_transcript(l3, make_long_segments(n=1, base=200))
    async with async_session_factory() as db:
        jid3 = (await index_service.index_service.create_index_job(
            db, ScopeContext(subject_id=SUBJECT_A), c3
        )).id
    unk = UnknownEmbed()
    async with async_session_factory() as db:
        await index_service.index_service.run_index_job(db, jid3, embed=unk)
    async with async_session_factory() as db:
        j3 = await db.get(ProcessingJob, jid3)
    emb_pc = await _pc_count(f"index:embed:{jid3}:")
    emb_ok = (
        j3.status == "failed" and j3.error_type == "provider_unknown"
        and unk.calls == 1 and emb_pc == 1
    )

    # (d) provider_unknown 任务普通 retry_job 被阻断（CA-07）：不重新外发/收费
    await _s3_reset()
    a4, _ = await create_lecture(SUBJECT_A, "ca07-retry-block")
    jid4 = await create_asr_job(a4)
    p4 = FakeASRProvider()
    p4.submit_unknown = True
    async with async_session_factory() as db:
        await job_service.run_job(db, jid4, provider=p4)
    retry_blocked = False
    try:
        async with async_session_factory() as db:
            await job_service.retry_job(db, jid4)
    except ServiceError as e:
        retry_blocked = (e.status_code == 409 and e.code == "provider_unknown")
    async with async_session_factory() as db:
        j4 = await db.get(ProcessingJob, jid4)
    retry_block_ok = (
        retry_blocked and j4.status == "failed" and j4.error_type == "provider_unknown"
        and p4.submit_calls == 1
    )

    # (e) 网关成功提交后、业务 Checkpoint 前失联：恢复复用持久化结果，不重新 submit（CA-07）
    await _s3_reset()
    a5, _ = await create_lecture(SUBJECT_A, "ca07-success-window")
    jid5 = await create_asr_job(a5)
    p5 = FakeASRProvider()
    import app.services.ai_call_gateway as gateway_mod
    orig_finish = gateway_mod._finish

    async def _crash_after_submit_success(*args, **kwargs):
        outcome = await orig_finish(*args, **kwargs)
        if getattr(args[1], "operation_type", None) == "asr_submit" and outcome.kind == "success":
            raise job_service.WorkerLost()
        return outcome

    gateway_mod._finish = _crash_after_submit_success
    crashed_window = False
    try:
        async with async_session_factory() as db:
            await job_service.run_job(db, jid5, provider=p5)
    except job_service.WorkerLost:
        crashed_window = True
    finally:
        gateway_mod._finish = orig_finish
    await expire_lease(jid5)
    p5b = FakeASRProvider()
    async with async_session_factory() as db:
        await job_service.run_job(db, jid5, provider=p5b)
    async with async_session_factory() as db:
        j5 = await db.get(ProcessingJob, jid5)
    rows5 = await _pc_rows(f"asr:submit:{jid5}")
    success_window_ok = (
        crashed_window and p5.submit_calls == 1 and p5b.submit_calls == 0
        and j5.status == "succeeded" and j5.output_version is not None
        and len(rows5) == 1 and rows5[0]["status"] == "succeeded"
    )

    record(
        "CA-07",
        "ASR提交/LLM汇总/embedding 供应商接收后中断进入 provider_unknown + 普通 retry_job 被阻断 + 成功至 Checkpoint 窗口恢复复用结果不重 submit",
        asr_ok and org_ok and emb_ok and retry_block_ok and success_window_ok,
        dict(asr_ok=asr_ok, asr_status=j.status, asr_calls=p.submit_calls,
             asr_row=rows[0] if rows else None,
             org_ok=org_ok, org_status=j2.status, summarize_row=srows[0] if srows else None,
             emb_ok=emb_ok, emb_status=j3.status, emb_calls=unk.calls, emb_pc=emb_pc,
             retry_block_ok=retry_block_ok, retry_blocked=retry_blocked,
             success_window_ok=success_window_ok, submit_calls_after=p5.submit_calls,
             recovery_submit_calls=p5b.submit_calls, states=[r["status"] for r in rows5]),
    )


# ── CA-10 IndexIntent 及 active 一致性 ────────────────────────────────
async def ca_10(current):
    await _s3_reset()

    # (a) 发布笔记触发 IndexIntent，消费意向经同一受控网关计费；重复消费幂等
    l, c = await create_lecture(SUBJECT_A, "ca10")
    _, v = await add_transcript(l, make_long_segments(n=1, base=200))
    jid = await make_organize_job(l, v)
    m = FakeOrganizeModel()
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, jid, model=m)
    async with async_session_factory() as db:
        intents = (await db.execute(select(IndexIntent))).scalars().all()
        intent_id = intents[0].id
    embed = CountingEmbed()
    async with async_session_factory() as db:
        intent = await db.get(IndexIntent, intent_id)
        await index_service.index_service.consume_intent(db, intent, embed=embed)
    intent_pc = await _pc_count("index:intent:")
    calls_after_first = embed.calls
    async with async_session_factory() as db:
        intent2 = await db.get(IndexIntent, intent_id)
        await index_service.index_service.consume_intent(db, intent2, embed=embed)
    intent_ok = (
        intent_pc >= 1 and calls_after_first == intent_pc and embed.calls == calls_after_first
    )

    # (b) 旧构建晚完成不顶替新 active（created_at 更早 → 仅 superseded）
    async with async_session_factory() as db:
        old_iv = IndexVersion(course_id=c, status="building", config={},
                              source_count=0, block_count=0,
                              created_at=now() - timedelta(hours=1))
        new_iv = IndexVersion(course_id=c, status="building", config={},
                              source_count=0, block_count=0, created_at=now())
        db.add(old_iv)
        db.add(new_iv)
        await db.flush()
        await index_service.index_service.activate(db, new_iv)
        await index_service.index_service.activate(db, old_iv)
        await db.commit()
        new_status = (await db.get(IndexVersion, new_iv.id)).status
        old_status = (await db.get(IndexVersion, old_iv.id)).status
    late_build_ok = new_status == "active" and old_status == "superseded"

    # (c) 构建中途失败保留旧 active（注入 UnknownEmbed 不破坏既有 active）
    async with async_session_factory() as db:
        active_before = (await db.execute(
            select(IndexVersion).where(IndexVersion.course_id == c, IndexVersion.status == "active")
        )).scalars().first()
        active_before_id = active_before.id
        active_before_blocks = active_before.block_count
        jid_fail = (await index_service.index_service.create_index_job(
            db, ScopeContext(subject_id=SUBJECT_A), c
        )).id
    unk = UnknownEmbed()
    async with async_session_factory() as db:
        await index_service.index_service.run_index_job(db, jid_fail, embed=unk)
    async with async_session_factory() as db:
        active_after = (await db.execute(
            select(IndexVersion).where(IndexVersion.course_id == c, IndexVersion.status == "active")
        )).scalars().first()
        fail_job = await db.get(ProcessingJob, jid_fail)
    fail_keeps_active = (
        active_after is not None and active_after.id == active_before_id
        and active_after.block_count == active_before_blocks
        and fail_job.status == "failed" and fail_job.error_type == "provider_unknown"
    )

    record(
        "CA-10",
        "IndexIntent 经同一受控网关计费且重复消费幂等 + 旧构建晚完成不顶替新 active + 中途失败保留旧版本",
        intent_ok and late_build_ok and fail_keeps_active,
        dict(intent_ok=intent_ok, intent_pc=intent_pc, embed_calls=embed.calls,
             late_build_ok=late_build_ok, new_status=new_status, old_status=old_status,
             fail_keeps_active=fail_keeps_active, active_after_id=str(active_after.id),
             fail_status=fail_job.status),
    )


# ── CA-15 所有真实外发统一受控 ────────────────────────────────────────
async def ca_15(current):
    await _s3_reset()

    # (a) ASR submit + poll 各为独立受控记录
    a, _ = await create_lecture(SUBJECT_A, "ca15-asr")
    jid = await create_asr_job(a)
    p = FakeASRProvider()
    async with async_session_factory() as db:
        await job_service.run_job(db, jid, provider=p)
    submit_rows = await _pc_rows(f"asr:submit:{jid}")
    poll_count = await _pc_count(f"asr:poll:{jid}:")
    asr_ok = (
        p.submit_calls == 1 and p.poll_calls == 1
        and len(submit_rows) == 1 and poll_count == 1
        and submit_rows[0]["status"] == "succeeded"
    )

    # (b) organize extract(2) + summarize(1) 各独立受控记录
    await _s3_reset()
    l2, _ = await create_lecture(SUBJECT_A, "ca15-org")
    _, v2 = await add_transcript(l2, make_long_segments(n=2, base=2000))
    jid2 = await make_organize_job(l2, v2)
    m2 = FakeOrganizeModel()
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, jid2, model=m2)
    extract_pc = await _pc_count(f"organize:extract:{jid2}:")
    summary_pc = await _pc_count(f"organize:summarize:{jid2}")
    org_ok = (
        len(m2.calls) == 2 and m2.summarize_calls == 1
        and extract_pc == 2 and summary_pc == 1
    )

    # (c) index embed 批次 == 调用记录数（逐单元受控）
    await _s3_reset()
    l3, c3 = await create_lecture(SUBJECT_A, "ca15-emb")
    await add_transcript(l3, make_long_segments(n=2, base=2000))
    async with async_session_factory() as db:
        jid3 = (await index_service.index_service.create_index_job(
            db, ScopeContext(subject_id=SUBJECT_A), c3
        )).id
    ce = CountingEmbed()
    async with async_session_factory() as db:
        await index_service.index_service.run_index_job(db, jid3, embed=ce)
    embed_pc = await _pc_count(f"index:embed:{jid3}:")
    emb_ok = ce.calls >= 1 and embed_pc == ce.calls

    # (d) 注入重试：poll 首次限流后成功 → 同一 operation 两次 attempt 独立记录
    await _s3_reset()
    a4, _ = await create_lecture(SUBJECT_A, "ca15-retry")
    jid4 = await create_asr_job(a4)
    rp = RetryOncePollProvider()
    orig_base = settings.CHECKPOINT_EXT_BACKOFF_BASE
    orig_cap = settings.CHECKPOINT_EXT_BACKOFF_CAP
    settings.CHECKPOINT_EXT_BACKOFF_BASE = 0.0
    settings.CHECKPOINT_EXT_BACKOFF_CAP = 0.0
    try:
        async with async_session_factory() as db:
            await job_service.run_job(db, jid4, provider=rp)
    finally:
        settings.CHECKPOINT_EXT_BACKOFF_BASE = orig_base
        settings.CHECKPOINT_EXT_BACKOFF_CAP = orig_cap
    retry_rows = await _pc_rows(f"asr:poll:{jid4}:0")
    retry_ok = (
        rp.submit_calls == 1 and rp.transport_calls == 2
        and len(retry_rows) == 2
        and [r["attempt_no"] for r in retry_rows] == [1, 2]
        and retry_rows[0]["status"] == "failed" and retry_rows[1]["status"] == "succeeded"
    )

    # (e) 真实 DashScope 适配器单次 poll = 单次 HTTP 请求（无内部循环）；
    #     真实整理/embedding 适配器在网关启用时不写旧 SQLite 账本（CA-15）。
    await _s3_reset()
    import httpx as _httpx
    from types import SimpleNamespace as _NS
    from app.services.asr_provider import DashScopeASRProvider
    from app.services.organize_model import RealOrganizeModel, ChunkInput
    from app.services.embedding_service import ClassroomEmbedding
    from app.services import trial_budget as _trial

    real_provider = DashScopeASRProvider()
    real_client = _httpx.AsyncClient
    http_calls = 0
    fake_segments = await FakeASRProvider().poll("local-review")
    orig_parse = real_provider._asr._parse_result

    def _asr_handler(request):
        nonlocal http_calls
        http_calls += 1
        return _httpx.Response(200, json={"output": {"task_status": "SUCCEEDED"}}, request=request)

    real_provider._asr._parse_result = lambda output: fake_segments
    _httpx.AsyncClient = lambda *a, **kw: real_client(*a, transport=_httpx.MockTransport(_asr_handler), **kw)
    try:
        spec_asr = CallSpec(operation_key="ca15:real-asr-poll", operation_type="asr_poll",
                            operation_family=rl.FAMILY_ASR_QUERY, provider="real",
                            subject_id=SUBJECT_A, is_idempotent=True)
        async with async_session_factory() as db:
            real_outcome = await run_operation(db, spec_asr, lambda: real_provider.poll("local-review"))
        real_pc = await _pc_count("ca15:real-asr-poll")
        real_asr_ok = http_calls == 1 and real_pc == 1 and real_outcome.kind == "success"
    finally:
        _httpx.AsyncClient = real_client
        real_provider._asr._parse_result = orig_parse

    import openai as _openai
    orig_client = _openai.AsyncOpenAI
    orig_reserve, orig_record = _trial.reserve_call, _trial.record_usage
    orig_key = settings.OPENAI_API_KEY
    sdk_calls = 0
    legacy_calls = 0

    def _fake_reserve(*args):
        nonlocal legacy_calls
        legacy_calls += 1
        return "local-review"

    class _MockChatClient:
        def __init__(self, **kwargs):
            self.chat = _NS(completions=self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **kwargs):
            nonlocal sdk_calls
            sdk_calls += 1
            return _NS(usage=None, choices=[_NS(
                finish_reason="stop",
                message=_NS(content='{"conclusions":[],"tasks":[]}'),
            )])

    _trial.reserve_call = _fake_reserve
    _trial.record_usage = lambda *args: None
    _openai.AsyncOpenAI = _MockChatClient
    settings.OPENAI_API_KEY = "local-review-no-network"
    try:
        model = RealOrganizeModel()
        chunk = ChunkInput(chunk_index=0, core=[{"seq_index": 0, "content": "review"}], context=[])
        spec_org = CallSpec(operation_key="ca15:real-model-ok", operation_type="organize_extract",
                            operation_family=rl.FAMILY_GEN, provider="real", subject_id=SUBJECT_A,
                            is_idempotent=False)
        async with async_session_factory() as db:
            org_outcome = await call_controlled(db, spec_org, lambda: model.extract_chunk(chunk), rng=_ZeroRng())
        real_org_ok = sdk_calls == 1 and legacy_calls == 0 and org_outcome.kind == "success"
    finally:
        _trial.reserve_call, _trial.record_usage = orig_reserve, orig_record
        _openai.AsyncOpenAI = orig_client
        settings.OPENAI_API_KEY = orig_key

    # 真实 embedding 适配器：网关启用时不写旧账本（CA-15）
    await _s3_reset()
    sdk_calls2 = 0
    legacy_calls2 = 0

    def _fake_reserve2(*args):
        nonlocal legacy_calls2
        legacy_calls2 += 1
        return "local-review"

    class _MockEmbedClient:
        def __init__(self, **kwargs):
            self.embeddings = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **kwargs):
            nonlocal sdk_calls2
            sdk_calls2 += 1
            dims = kwargs.get("dimensions") or settings.INDEX_EMBEDDING_DIMENSIONS
            texts = kwargs.get("input") or []
            return _NS(usage=None, data=[_NS(index=i, embedding=[0.0] * dims) for i in range(len(texts))])

    import app.services.embedding_service as emb_mod
    orig_emb_client = emb_mod.AsyncOpenAI
    _trial.reserve_call = _fake_reserve2
    _trial.record_usage = lambda *args: None
    # ClassroomEmbedding 在模块级 import AsyncOpenAI，需 patch 模块引用（而非 openai 全局）。
    emb_mod.AsyncOpenAI = _MockEmbedClient
    settings.OPENAI_API_KEY = "local-review-no-network"
    try:
        emb = ClassroomEmbedding()
        spec_emb = CallSpec(operation_key="ca15:real-embed", operation_type="index_embed",
                            operation_family=rl.FAMILY_EMBED, provider="real", subject_id=SUBJECT_A,
                            is_idempotent=False)
        async with async_session_factory() as db:
            emb_outcome = await call_controlled(db, spec_emb, lambda: emb.embed_batch(["review text"]), rng=_ZeroRng())
        real_emb_ok = sdk_calls2 == 1 and legacy_calls2 == 0 and emb_outcome.kind == "success"
    finally:
        _trial.reserve_call, _trial.record_usage = orig_reserve, orig_record
        emb_mod.AsyncOpenAI = orig_emb_client
        settings.OPENAI_API_KEY = orig_key

    record(
        "CA-15",
        "ASR submit/每次 poll、extract、summary、embed、IndexIntent 均逐次受控计费 + 注入重试 attempt_no 递增 + 真实适配器单次查询/无旧账双写",
        asr_ok and org_ok and emb_ok and retry_ok and real_asr_ok and real_org_ok and real_emb_ok,
        dict(asr_ok=asr_ok, submit_calls=p.submit_calls, poll_calls=p.poll_calls, poll_count=poll_count,
             org_ok=org_ok, extract_calls=len(m2.calls), extract_pc=extract_pc, summary_pc=summary_pc,
             emb_ok=emb_ok, embed_calls=ce.calls, embed_pc=embed_pc,
             retry_ok=retry_ok, retry_attempts=[r["attempt_no"] for r in retry_rows],
             real_asr_ok=real_asr_ok, http_calls=http_calls, real_pc=real_pc,
             real_org_ok=real_org_ok, org_sdk_calls=sdk_calls, org_legacy_calls=legacy_calls,
             real_emb_ok=real_emb_ok, emb_sdk_calls=sdk_calls2, emb_legacy_calls=legacy_calls2),
    )


# ── CA-17 持久等待与本地并发 permit ───────────────────────────────────
async def ca_17(current):
    await _s3_reset()

    # (a) 2 个 permit 占用，第 3 任务控制等待：不占 permit/预算/外部 attempt
    await _occupy_permits(2)
    a, _ = await create_lecture(SUBJECT_A, "ca17")
    jid = await create_asr_job(a)
    p = FakeASRProvider()
    async with async_session_factory() as db:
        await job_service.run_job(db, jid, provider=p)
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, jid)
        wait_status = j.status
        wait_reason = j.wait_reason
        wait_elapsed = j.control_wait_elapsed_seconds
    submit_pc = await _pc_count(f"asr:submit:{jid}")
    st = await _budget_state(SUBJECT_A)
    async with async_session_factory() as db:
        in_flight = await permit.count_in_flight(db)
    wait_ok = (
        wait_status == "retry_wait" and wait_reason == "concurrency_limit"
        and wait_elapsed == settings.CHECKPOINT_PROVIDER_PERMIT_TTL_SECONDS
        and p.submit_calls == 0 and submit_pc == 0
        and st["subject_daily"]["reserved_micro"] == 0
        and st["subject_daily"]["settled_micro"] == 0
        and in_flight == 2
    )

    # (b) 累计控制等待耗尽 → 明确失败 control_wait_exhausted
    await _flush_redis()
    async with async_session_factory() as db:
        j2 = await db.get(ProcessingJob, jid)
        j2.control_wait_elapsed_seconds = settings.CHECKPOINT_CONTROL_WAIT_SECONDS
        await db.commit()
    async with async_session_factory() as db:
        await job_service.run_job(db, jid, provider=p)
    async with async_session_factory() as db:
        j3 = await db.get(ProcessingJob, jid)
    exhausted_ok = (
        j3.status == "failed" and j3.error_type == "control_wait_exhausted"
        and j3.error_code == "control_wait_exhausted"
    )

    # (c) 无 owner 的 IndexIntent/直接调用不得旁路 permit（CA-17）：第 3 个并发同样控制等待
    await _s3_reset()
    await _occupy_permits(2)
    third_calls = 0

    async def _no_owner_transport():
        nonlocal third_calls
        third_calls += 1
        return {"ok": True}

    no_owner_wait = False
    try:
        async with async_session_factory() as db:
            spec = CallSpec(operation_key=f"ca17:no-owner:{uuid.uuid4().hex[:8]}",
                            operation_type="index_embed", operation_family=rl.FAMILY_EMBED,
                            provider="fake", subject_id=SUBJECT_A, is_idempotent=False)
            await run_operation(db, spec, _no_owner_transport)
    except ControlWaitError:
        no_owner_wait = True
    no_owner_ok = no_owner_wait and third_calls == 0

    record(
        "CA-17",
        "2 个在途 permit 后第 3 任务控制等待不占 permit/预算/attempt + 累计等待耗尽明确失败 + 无 owner 调用不旁路 permit",
        wait_ok and exhausted_ok and no_owner_ok,
        dict(wait_ok=wait_ok, wait_status=wait_status, wait_reason=wait_reason,
             wait_elapsed=wait_elapsed, submit_calls=p.submit_calls, submit_pc=submit_pc,
             in_flight=in_flight,
             reserved=st["subject_daily"]["reserved_micro"],
             exhausted_ok=exhausted_ok, final_status=j3.status, final_type=j3.error_type,
             no_owner_ok=no_owner_ok, no_owner_wait=no_owner_wait, third_calls=third_calls),
    )


# ── CA-18 多进程预算预留与幂等结算 ─────────────────────────────────────
async def ca_18(current):
    await _s3_reset()

    # (a) 并发申请主体最后额度：一成一败，且失败后无出网（无额外预留记录）
    cap_subject = settings.BUDGET_SUBJECT_DAILY_CNY * settings.BUDGET_MICRO_PER_CNY
    await _set_account("subject_daily", SUBJECT_A, reserved_micro=cap_subject - budget.reserve_min_micro())
    r1, r2 = await asyncio.gather(
        _reserve_once(SUBJECT_A, "ca18:race:a", 1),
        _reserve_once(SUBJECT_A, "ca18:race:b", 1),
    )
    async with async_session_factory() as db:
        res_count = (await db.execute(select(func.count(BudgetReservation.id)))).scalar_one()
    race_ok = sorted([r1, r2]) == ["exhausted", "ok"] and res_count == 1

    # (b) 幂等结算：同 attempt 结算两次不重复扣款
    await _s3_reset()
    aid = uuid.uuid4()
    async with async_session_factory() as db:
        amt = await budget.reserve_budget(
            db, attempt_id=aid, operation_key="ca18:settle", attempt_no=1, subject_id=SUBJECT_A
        )
        await budget.settle_budget(db, attempt_id=aid, settled_micro=amt, usage_state="confirmed_sent")
        await db.commit()
        await budget.settle_budget(db, attempt_id=aid, settled_micro=amt, usage_state="confirmed_sent")
        await db.commit()
    st = await _budget_state(SUBJECT_A)
    idem_ok = st["subject_daily"]["settled_micro"] == amt

    # (c) 换主体/换入口不绕总额：全局日账户跨主体共享
    await _s3_reset()
    async with async_session_factory() as db:
        await budget.reserve_budget(
            db, attempt_id=uuid.uuid4(), operation_key="asr:submit:x", attempt_no=1, subject_id=SUBJECT_B
        )
        await db.commit()
    st_global = await _budget_state(SUBJECT_A)
    cross_ok = (
        st_global["global_daily"]["reserved_micro"] == budget.reserve_min_micro()
        and st_global["subject_daily"]["reserved_micro"] == 0
    )

    # (d) 金额定点整数无浮点漂移
    int_ok = isinstance(amt, int) and isinstance(st["subject_daily"]["settled_micro"], int)

    # (e) 两个独立进程抢最后主体/全局额度：仅一个成功，拒绝后无额外预留（CA-18 独立进程证据）
    await _s3_reset()
    cap_subject2 = settings.BUDGET_SUBJECT_DAILY_CNY * settings.BUDGET_MICRO_PER_CNY
    await _set_account("subject_daily", SUBJECT_A, reserved_micro=cap_subject2 - budget.reserve_min_micro())
    p1 = _launch_budget_worker(SUBJECT_A, "ca18:proc:a", 1)
    p2 = _launch_budget_worker(SUBJECT_A, "ca18:proc:b", 1)
    out1 = _worker_result(p1)
    out2 = _worker_result(p2)
    async with async_session_factory() as db:
        proc_res_count = (await db.execute(
            select(func.count(BudgetReservation.id)).where(
                BudgetReservation.operation_key.in_(["ca18:proc:a", "ca18:proc:b"])
            )
        )).scalar_one()
    proc_race_ok = sorted([out1, out2]) == ["exhausted", "ok"] and proc_res_count == 1

    record(
        "CA-18",
        "并发预留不超可用额 + 幂等结算不重复扣款 + 换主体不绕全局总额 + 整数微元无浮点 + 两独立进程抢最后额度仅一成功",
        race_ok and idem_ok and cross_ok and int_ok and proc_race_ok,
        dict(race_ok=race_ok, r1=r1, r2=r2, res_count=res_count,
             idem_ok=idem_ok, settled_micro=st["subject_daily"]["settled_micro"], amt=amt,
             cross_ok=cross_ok, global_reserved=st_global["global_daily"]["reserved_micro"],
             int_ok=int_ok, proc_race_ok=proc_race_ok,
             proc_out1=out1, proc_out2=out2, proc_res_count=proc_res_count),
    )


# ── CA-19 计量未知/失败/跨日/旧账迁移 ──────────────────────────────────
async def ca_19(current):
    await _s3_reset()

    # (a) confirmed_not_sent 释放预留
    aid = uuid.uuid4()
    async with async_session_factory() as db:
        amt = await budget.reserve_budget(db, attempt_id=aid, operation_key="ca19:a", attempt_no=1, subject_id=SUBJECT_A)
        await budget.settle_budget(db, attempt_id=aid, settled_micro=0, usage_state="confirmed_not_sent")
        await db.commit()
    st = await _budget_state(SUBJECT_A)
    not_sent_ok = st["subject_daily"]["settled_micro"] == 0 and st["subject_daily"]["reserved_micro"] == 0

    # (b) unknown 保留保守预留（settled = reserved，不释放）
    await _s3_reset()
    aid2 = uuid.uuid4()
    async with async_session_factory() as db:
        amt2 = await budget.reserve_budget(db, attempt_id=aid2, operation_key="ca19:b", attempt_no=1, subject_id=SUBJECT_A)
        await budget.settle_budget(db, attempt_id=aid2, settled_micro=amt2, usage_state="unknown")
        await db.commit()
    st2 = await _budget_state(SUBJECT_A)
    unknown_ok = st2["subject_daily"]["settled_micro"] == amt2 and st2["subject_daily"]["reserved_micro"] == 0

    # (c) confirmed_sent 计 settled = reserved
    await _s3_reset()
    aid3 = uuid.uuid4()
    async with async_session_factory() as db:
        amt3 = await budget.reserve_budget(db, attempt_id=aid3, operation_key="ca19:c", attempt_no=1, subject_id=SUBJECT_A)
        await budget.settle_budget(db, attempt_id=aid3, settled_micro=amt3, usage_state="confirmed_sent")
        await db.commit()
    st3 = await _budget_state(SUBJECT_A)
    sent_ok = st3["subject_daily"]["settled_micro"] == amt3

    # (d) 跨上海午夜结算归原日账户
    await _s3_reset()
    past_day = budget.shanghai_day(now() - timedelta(days=2))
    aid4 = uuid.uuid4()
    async with async_session_factory() as db:
        amt4 = await budget.reserve_budget(db, attempt_id=aid4, operation_key="ca19:d", attempt_no=1,
                                           subject_id=SUBJECT_A, day=past_day)
        await budget.settle_budget(db, attempt_id=aid4, settled_micro=amt4, usage_state="confirmed_sent")
        await db.commit()
    past_state = await _budget_state(SUBJECT_A, day=past_day)
    today_state = await _budget_state(SUBJECT_A)
    cross_ok = (
        past_state["subject_daily"]["settled_micro"] == amt4
        and today_state["subject_daily"]["settled_micro"] == 0
    )

    # (e) 旧试用账本迁移：累计 10 元不重置，可对账，且只写 trial_total
    await _s3_reset()
    import sqlite3
    import tempfile
    tmp = Path(tempfile.gettempdir()) / f"yuan-trial-{uuid.uuid4().hex}.sqlite3"
    conn = sqlite3.connect(str(tmp))
    conn.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, reserved_cny REAL)")
    conn.execute("INSERT INTO calls (reserved_cny) VALUES (3.5)")
    conn.execute("INSERT INTO calls (reserved_cny) VALUES (1.25)")
    conn.commit()
    conn.close()
    orig_ledger = settings.CLASSROOM_TRIAL_LEDGER
    settings.CLASSROOM_TRIAL_LEDGER = str(tmp)
    try:
        async with async_session_factory() as db:
            migrated = await budget.migrate_legacy_trial(db, SUBJECT_A)
            await db.commit()
        st_trial = await _budget_state(SUBJECT_A)
        async with async_session_factory() as db:
            migrated2 = await budget.migrate_legacy_trial(db, SUBJECT_A)
            await db.commit()
        st_trial2 = await _budget_state(SUBJECT_A)
        expected = 4_750_000  # ceil((3.5 + 1.25) * 1e6)
        legacy_ok = (
            migrated == expected and migrated2 == expected
            and st_trial["trial_total"]["settled_micro"] == expected
            and st_trial2["trial_total"]["settled_micro"] == expected
            and st_trial["subject_daily"]["settled_micro"] == 0
            and st_trial["global_daily"]["settled_micro"] == 0
        )
    finally:
        settings.CLASSROOM_TRIAL_LEDGER = orig_ledger
        tmp.unlink(missing_ok=True)

    # (f) 应用实际任务入口首次预留前自动迁移旧账（CA-19）：旧账已 10 元达到试用累计上限，
    #     新 submit/poll 被预算拦截（累计不重置），trial_total 仍为 10 元而非只记新增。
    await _s3_reset()
    import tempfile as _tempfile
    tmp2 = Path(_tempfile.gettempdir()) / f"yuan-trial-entry-{uuid.uuid4().hex}.sqlite3"
    conn2 = sqlite3.connect(str(tmp2))
    conn2.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, reserved_cny REAL)")
    conn2.execute("INSERT INTO calls (reserved_cny) VALUES (10)")
    conn2.commit()
    conn2.close()
    orig_ledger2 = settings.CLASSROOM_TRIAL_LEDGER
    settings.CLASSROOM_TRIAL_LEDGER = str(tmp2)
    try:
        a6, _ = await create_lecture(SUBJECT_A, "ca19-entry")
        jid6 = await create_asr_job(a6)
        p6 = FakeASRProvider()
        async with async_session_factory() as db:
            await job_service.run_job(db, jid6, provider=p6)
        async with async_session_factory() as db:
            j6 = await db.get(ProcessingJob, jid6)
        st6 = await _budget_state(SUBJECT_A)
        entry_ok = (
            p6.submit_calls == 0 and p6.poll_calls == 0
            and j6.status == "failed" and j6.error_type == "budget_exhausted"
            and st6["trial_total"]["settled_micro"] == 10_000_000
            and st6["subject_daily"]["settled_micro"] == 0
        )
    finally:
        settings.CLASSROOM_TRIAL_LEDGER = orig_ledger2
        tmp2.unlink(missing_ok=True)

    record(
        "CA-19",
        "confirmed_not_sent 释放 / unknown 保留 / confirmed_sent 计费 / 跨午夜归原日 / 旧账迁移累计不重置 + 应用入口自动迁移并拦截超上限新调用",
        not_sent_ok and unknown_ok and sent_ok and cross_ok and legacy_ok and entry_ok,
        dict(not_sent_ok=not_sent_ok, unknown_ok=unknown_ok, sent_ok=sent_ok,
             cross_ok=cross_ok, past_day=str(past_day),
             past_settled=past_state["subject_daily"]["settled_micro"],
             today_settled=today_state["subject_daily"]["settled_micro"],
             legacy_ok=legacy_ok, migrated=migrated,
             trial_settled=st_trial["trial_total"]["settled_micro"],
             entry_ok=entry_ok, entry_submit=p6.submit_calls, entry_poll=p6.poll_calls,
             entry_status=j6.status, entry_trial_settled=st6["trial_total"]["settled_micro"]),
    )


# ── CA-20 错误分类与等待提示 ──────────────────────────────────────────
async def ca_20(current):
    await _s3_reset()
    cls = backoff_mod.classify_exception

    def _err(etype, retryable, code=None):
        return ProviderError(etype, "m", retryable, code=code or etype)

    # 分类：限流/5xx 可重试；永久错误停止；非幂等超时/未知 → unknown
    tax_ok = (
        cls(_err("rate_limited", True, code="rate_limited"), is_idempotent=False) == "retryable"
        and cls(_err("provider_error", True), is_idempotent=False) == "retryable"
        and cls(_err("config_error", False), is_idempotent=False) == "permanent"
        and cls(_err("no_speech", False), is_idempotent=False) == "permanent"
        and cls(ModelError("invalid_result", "非法 schema", retryable=False), is_idempotent=False) == "permanent"
        and cls(_err("timeout", True, code="timeout"), is_idempotent=False) == "unknown"
        and cls(_err("timeout", True, code="timeout"), is_idempotent=True) == "retryable"
        and cls(ProviderUnknownError(), is_idempotent=False) == "unknown"
    )

    # Retry-After 解析：秒数 / HTTP-date / 坏值 / 负数
    pra = backoff_mod.parse_retry_after
    ra_ok = (
        pra("120") == 120.0
        and pra(30) == 30.0
        and pra("not-a-date") is None
        and pra(None) is None
        and pra(-5) is None
        and pra("Wed, 21 Oct 2027 07:28:00 GMT") > 0
    )

    # 退避公式：delay = max(full_jitter, retry_after, local_limiter_delay)
    cb = backoff_mod.compute_backoff
    backoff_ok = (
        cb(1, rng=_ZeroRng()) == 0.0
        and cb(1, retry_after=5, rng=_ZeroRng()) == 5.0
        and cb(2, retry_after=7, local_limiter_delay=9, rng=_ZeroRng()) == 9.0
        and 0.0 <= cb(5, rng=random.Random(42)) <= 30.0
    )

    # Retry-After 超过期限：出网一次后 deadline_exhausted，不再重试
    class _T:
        def __init__(self):
            self.calls = 0

        async def __call__(self):
            self.calls += 1
            raise RetryAfterProviderError()

    t = _T()
    async with async_session_factory() as db:
        spec = CallSpec(operation_key=f"ca20:{uuid.uuid4().hex[:8]}", operation_type="organize_extract",
                        operation_family=rl.FAMILY_GEN, provider="fake", subject_id=SUBJECT_A)
        outcome = await call_controlled(db, spec, t, rng=_ZeroRng())
    deadline_ok = (
        t.calls == 1 and outcome.kind == "permanent"
        and outcome.error_code == "deadline_exhausted"
    )

    # (d) 真实整理 adapter：429 保留 Retry-After=1200（>900s 总期限）→ 仅 1 次 SDK 调用后 deadline_exhausted，
    #     不截短为立即重试 3 次；非幂等连接/超时转 unknown 且不重发（CA-20）。
    await _s3_reset()
    import httpx as _httpx
    import openai as _openai
    from types import SimpleNamespace as _NS
    from app.services.organize_model import RealOrganizeModel, ChunkInput
    from app.services import trial_budget as _trial

    orig_client = _openai.AsyncOpenAI
    orig_reserve, orig_record = _trial.reserve_call, _trial.record_usage
    orig_key = settings.OPENAI_API_KEY
    mode = "429"
    sdk_calls = 0
    legacy_calls = 0

    def _fake_reserve(*args):
        nonlocal legacy_calls
        legacy_calls += 1
        return "local-review"

    class _MockErrClient:
        def __init__(self, **kwargs):
            self.chat = _NS(completions=self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **kwargs):
            nonlocal sdk_calls
            sdk_calls += 1
            request = _httpx.Request("POST", "https://local-review.invalid")
            if mode == "429":
                response = _httpx.Response(429, headers={"Retry-After": "1200"}, request=request)
                raise _openai.APIStatusError("review rate limit", response=response, body={})
            raise _openai.APIConnectionError(request=request)

    _trial.reserve_call = _fake_reserve
    _trial.record_usage = lambda *args: None
    _openai.AsyncOpenAI = _MockErrClient
    settings.OPENAI_API_KEY = "local-review-no-network"
    real_429_ok = False
    real_conn_ok = False
    try:
        model = RealOrganizeModel()
        chunk = ChunkInput(chunk_index=0, core=[{"seq_index": 0, "content": "review"}], context=[])
        spec_429 = CallSpec(operation_key="ca20:real-429", operation_type="organize_extract",
                            operation_family=rl.FAMILY_GEN, provider="real", subject_id=SUBJECT_A,
                            is_idempotent=False)
        async with async_session_factory() as db:
            outcome_429 = await call_controlled(db, spec_429, lambda: model.extract_chunk(chunk), rng=_ZeroRng())
        real_429_ok = (
            sdk_calls == 1 and legacy_calls == 0
            and outcome_429.kind == "permanent" and outcome_429.error_code == "deadline_exhausted"
        )
        mode = "connection"
        sdk_calls = 0
        legacy_calls = 0
        spec_conn = CallSpec(operation_key="ca20:real-conn", operation_type="organize_extract",
                             operation_family=rl.FAMILY_GEN, provider="real", subject_id=SUBJECT_A,
                             is_idempotent=False)
        async with async_session_factory() as db:
            outcome_conn = await call_controlled(db, spec_conn, lambda: model.extract_chunk(chunk), rng=_ZeroRng())
        real_conn_ok = (
            sdk_calls == 1 and legacy_calls == 0
            and outcome_conn.kind == "unknown"
        )
    finally:
        _trial.reserve_call, _trial.record_usage = orig_reserve, orig_record
        _openai.AsyncOpenAI = orig_client
        settings.OPENAI_API_KEY = orig_key

    # (e) 真实 ASR submit：429 保留 Retry-After=1200（>900s 期限 → 仅 1 次 HTTP 后 deadline_exhausted，
    #     不截短为立即重试）；401 永久停止；响应丢失（连接/超时）→ unknown 阻断普通重试（CA-07/CA-20）。
    await _s3_reset()
    import httpx as _httpx_asr
    from types import SimpleNamespace as _NS_asr
    from app.services.asr_provider import DashScopeASRProvider as _RealASRProvider
    from app.services.dashscope_asr_service import DashScopeASRService as _RealASRService
    import app.services.media_service as _media

    orig_httpx_client = _httpx_asr.AsyncClient
    orig_media_probe = _media.probe
    _media.probe = lambda _p: _NS_asr(duration_ms=1000, size=100)
    asr_submit_ok = {}
    asr_submit_detail = {}
    try:
        for _mode in ("429", "connection", "401"):
            await _s3_reset()
            _requests = []

            def _handler(request):
                _requests.append(request.method)
                if _mode == "connection":
                    raise _httpx_asr.ReadTimeout("response lost after submit", request=request)
                return _httpx_asr.Response(int(_mode), headers={"Retry-After": "1200"},
                                          json={"code": "local-test"})

            def _client(*args, **kw):
                kw["transport"] = _httpx_asr.MockTransport(_handler)
                return orig_httpx_client(*args, **kw)

            _httpx_asr.AsyncClient = _client
            _rp = _RealASRProvider()
            _rp._asr = _RealASRService()
            _rp._asr.api_key = "local-test-key"
            _rp._oss = _NS_asr(is_available=True, upload_audio_object=lambda _p: "test-object",
                               sign_url=lambda _k: "https://local.invalid/audio",
                               delete_object_key=lambda _k: True)
            async with async_session_factory() as db:
                _spec = CallSpec(operation_key=f"ca20:asr-{_mode}", operation_type="asr_submit",
                                 operation_family=rl.FAMILY_ASR_SUBMIT, provider="real",
                                 subject_id=SUBJECT_A, is_idempotent=False)
                _out = await run_operation(db, _spec, lambda: _rp.submit("local-fake.wav"),
                                           rng=_ZeroRng())
            if _mode == "429":
                _ok = (
                    len(_requests) == 1 and _out.kind == "permanent"
                    and _out.error_code == "deadline_exhausted"
                    and getattr(_out.error, "retry_after_seconds", None) == 1200.0
                )
            elif _mode == "connection":
                _ok = len(_requests) == 1 and _out.kind == "unknown"
            else:  # 401
                _ok = len(_requests) == 1 and _out.kind == "permanent" and _out.error_code == "invalid_auth"
            asr_submit_ok[_mode] = _ok
            asr_submit_detail[_mode] = dict(kind=_out.kind, code=_out.error_code,
                                            requests=len(_requests),
                                            retry_after=getattr(_out.error, "retry_after_seconds", None))
    finally:
        _httpx_asr.AsyncClient = orig_httpx_client
        _media.probe = orig_media_probe
    asr_submit_real_ok = all(asr_submit_ok.values())

    record(
        "CA-20",
        "错误分类仅安全错误重试 + Retry-After 秒/日期/坏值解析 + 退避取最大值 + 超期停止不重试 + 真实 adapter 429/连接语义 + 真实 ASR submit 429/401/响应丢失语义",
        tax_ok and ra_ok and backoff_ok and deadline_ok and real_429_ok and real_conn_ok and asr_submit_real_ok,
        dict(tax_ok=tax_ok, ra_ok=ra_ok, backoff_ok=backoff_ok,
             deadline_ok=deadline_ok, transport_calls=t.calls,
             outcome_kind=outcome.kind, outcome_code=outcome.error_code,
             real_429_ok=real_429_ok, real_429_kind=outcome_429.kind,
             real_429_code=outcome_429.error_code,
             real_conn_ok=real_conn_ok, real_conn_kind=outcome_conn.kind,
             asr_submit_real_ok=asr_submit_real_ok, asr_submit_detail=asr_submit_detail),
    )


# ── CA-21 退避公式与总尝试上限 ────────────────────────────────────────
async def ca_21(current):
    await _s3_reset()

    # (a) 连续明确临时失败 → 首次含在内 ≤3 次，之后 attempt_limit_exhausted
    t = _AlwaysFailTransport()
    op_key = "ca21:unit:a"
    async with async_session_factory() as db:
        spec = CallSpec(operation_key=op_key, operation_type="organize_extract",
                        operation_family=rl.FAMILY_GEN, provider="fake", subject_id=SUBJECT_A)
        outcome = await call_controlled(db, spec, t, rng=_ZeroRng())
    rows = await _pc_rows(op_key)
    a_ok = (
        outcome.kind == "permanent" and outcome.error_code == "attempt_limit_exhausted"
        and t.calls == 3 and [r["attempt_no"] for r in rows] == [1, 2, 3]
    )

    # (b) 重启/普通重试不清零：再次调用 attempt_no=4 → permanent 且不再出网
    t2 = _AlwaysFailTransport()
    async with async_session_factory() as db:
        spec2 = CallSpec(operation_key=op_key, operation_type="organize_extract",
                         operation_family=rl.FAMILY_GEN, provider="fake", subject_id=SUBJECT_A)
        outcome2 = await call_controlled(db, spec2, t2, rng=_ZeroRng())
    b_ok = (
        outcome2.kind == "permanent" and outcome2.error_code == "attempt_limit_exhausted"
        and t2.calls == 0
    )

    # (c) 无三层重试放大：实际 transport 次数 == 尝试上限（网关唯一重试层）
    no_amp_ok = t.calls == 3

    # (d) 持久退避：第一次返回 retry 落库 retry_at，新 session 立即续跑同一 operation
    #     继续等待、不重复出网（CA-21：重启/普通重试遵守持久等待时间，等待不清零）。
    await _s3_reset()
    key = "ca21:persistent-retry-after"

    async def _temporary_error():
        err = ProviderError("rate_limited", "not executed", retryable=True)
        err.retry_after_seconds = 60
        raise err

    async with async_session_factory() as db:
        spec_d = CallSpec(operation_key=key, operation_type="organize_extract",
                          operation_family=rl.FAMILY_GEN, provider="fake", subject_id=SUBJECT_A)
        first = await run_operation(db, spec_d, _temporary_error, rng=_ZeroRng())
    async with async_session_factory() as db:
        pc_d = (await db.execute(select(ProviderCall).where(ProviderCall.operation_key == key))).scalars().first()
        stored_retry_at = pc_d.retry_at if pc_d is not None else None
    second_calls = 0

    async def _premature_second():
        nonlocal second_calls
        second_calls += 1
        return {"ok": True}

    async with async_session_factory() as db:
        spec_d2 = CallSpec(operation_key=key, operation_type="organize_extract",
                           operation_family=rl.FAMILY_GEN, provider="fake", subject_id=SUBJECT_A)
        second = await run_operation(db, spec_d2, _premature_second, rng=_ZeroRng())
    persist_ok = (
        first.kind == "retry" and first.backoff_seconds == 60.0
        and stored_retry_at is not None and stored_retry_at > now()
        and second.kind == "retry" and second_calls == 0 and (second.backoff_seconds or 0) > 0
    )

    # (e) 失败提交后失联窗口：结算、失败状态、retry_at 原子持久化（CA-21）。
    #     若「先提交失败状态、后写 retry_at」间失联，retry_at 会为 null，恢复立即重发绕过等待。
    await _s3_reset()
    key_e = "ca21:loss-after-failed-commit"
    count_e = 0

    async def _temp_loss():
        nonlocal count_e
        count_e += 1
        raise ProviderError("rate_limited", "confirmed not executed", retryable=True,
                            retry_after_seconds=60)

    async with async_session_factory() as db:
        original_commit_e = db.commit

        async def _commit_then_lose():
            await original_commit_e()
            pc_e = (await db.execute(select(ProviderCall).where(
                ProviderCall.operation_key == key_e))).scalars().first()
            if pc_e is not None and pc_e.status == "failed" and pc_e.retry_at is None:
                raise job_service.WorkerLost()

        db.commit = _commit_then_lose
        try:
            await run_operation(db, CallSpec(operation_key=key_e, operation_type="organize_extract",
                                             operation_family=rl.FAMILY_GEN, provider="fake",
                                             subject_id=SUBJECT_A), _temp_loss, rng=_ZeroRng())
        except job_service.WorkerLost:
            pass

    async with async_session_factory() as db:
        pc_e = (await db.execute(select(ProviderCall).where(
            ProviderCall.operation_key == key_e))).scalars().first()
        stored_retry_at_e = pc_e.retry_at if pc_e is not None else None

    async with async_session_factory() as db:
        spec_e2 = CallSpec(operation_key=key_e, operation_type="organize_extract",
                           operation_family=rl.FAMILY_GEN, provider="fake", subject_id=SUBJECT_A)
        recovered_e = await run_operation(db, spec_e2, _temp_loss, rng=_ZeroRng())
    atomic_loss_ok = (
        stored_retry_at_e is not None and stored_retry_at_e > now()
        and count_e == 1 and recovered_e.kind == "retry" and (recovered_e.backoff_seconds or 0) > 0
    )

    # (f) 终止决定持久化与恢复（幂等/非幂等通用）：首次 Retry-After=1200 判定超期 → 持久化
    #     deadline_exhausted（保留原供应商 error_code），换 session 恢复同一 operation_key 不再
    #     请求，两类操作总请求数均保持 1（CA-21：幂等不得绕过已确定的停止决定）。
    await _s3_reset()
    term_parts = {}

    async def _run_term_recovery(tag, is_idempotent):
        key = f"ca21:terminal-retry-after:{tag}"
        calls = 0

        async def _terminal_error():
            nonlocal calls
            calls += 1
            raise ProviderError("rate_limited", "not executed", retryable=True,
                                retry_after_seconds=1200)

        def _spec():
            return CallSpec(
                operation_key=key,
                operation_type="asr_poll" if is_idempotent else "asr_submit",
                operation_family=rl.FAMILY_ASR_QUERY if is_idempotent else rl.FAMILY_ASR_SUBMIT,
                provider="fake", subject_id=SUBJECT_A, is_idempotent=is_idempotent,
            )

        async with async_session_factory() as db:
            first = await run_operation(db, _spec(), _terminal_error, rng=_ZeroRng())
        async with async_session_factory() as db:
            row = (await db.execute(select(ProviderCall).where(
                ProviderCall.operation_key == key))).scalars().first()
            stored = dict(status=row.status, error_code=row.error_code, retry_at=row.retry_at)
        async with async_session_factory() as db:
            second = await run_operation(db, _spec(), _terminal_error, rng=_ZeroRng())
        return dict(first_kind=first.kind, first_code=first.error_code, stored=stored,
                    requests=calls, second_kind=second.kind, second_code=second.error_code)

    term_parts["non_idempotent"] = await _run_term_recovery("nonidem", False)
    term_parts["idempotent"] = await _run_term_recovery("idem", True)
    term_recovery_ok = all(
        p["first_kind"] == "permanent" and p["first_code"] == "deadline_exhausted"
        and p["stored"]["status"] == "deadline_exhausted" and p["stored"]["error_code"] == "provider_error"
        and p["stored"]["retry_at"] is None
        and p["requests"] == 1 and p["second_kind"] == "permanent" and p["second_code"] == "deadline_exhausted"
        for p in term_parts.values()
    )

    # (g) 次数耗尽终止决定恢复：attempt_limit_exhausted 持久化后，幂等/非幂等恢复均不再重发，
    #     总请求数保持 3（CA-21 三次上限不得绕过）。
    await _s3_reset()
    limit_parts = {}

    async def _run_limit_recovery(tag, is_idempotent):
        key = f"ca21:attempt-limit:{tag}"
        calls = 0

        async def _fail():
            nonlocal calls
            calls += 1
            raise ProviderError("provider_error", "供应商内部错误", retryable=True)

        def _spec():
            return CallSpec(
                operation_key=key,
                operation_type="asr_poll" if is_idempotent else "asr_submit",
                operation_family=rl.FAMILY_ASR_QUERY if is_idempotent else rl.FAMILY_ASR_SUBMIT,
                provider="fake", subject_id=SUBJECT_A, is_idempotent=is_idempotent,
            )

        async with async_session_factory() as db:
            first = await call_controlled(db, _spec(), _fail, rng=_ZeroRng())
        async with async_session_factory() as db:
            row = (await db.execute(select(ProviderCall).where(
                ProviderCall.operation_key == key).order_by(
                ProviderCall.attempt_no.desc()))).scalars().first()
            stored = dict(status=row.status, error_code=row.error_code, retry_at=row.retry_at)
        async with async_session_factory() as db:
            second = await run_operation(db, _spec(), _fail, rng=_ZeroRng())
        return dict(first_kind=first.kind, first_code=first.error_code, stored=stored,
                    requests=calls, second_kind=second.kind, second_code=second.error_code)

    cap_names_g = ("BUDGET_SUBJECT_DAILY_CNY", "BUDGET_GLOBAL_DAILY_CNY", "BUDGET_TRIAL_TOTAL_CNY")
    orig_caps_g = {name: getattr(settings, name) for name in cap_names_g}
    for name in cap_names_g:
        setattr(settings, name, 1000)
    try:
        limit_parts["non_idempotent"] = await _run_limit_recovery("nonidem", False)
        limit_parts["idempotent"] = await _run_limit_recovery("idem", True)
    finally:
        for name, value in orig_caps_g.items():
            setattr(settings, name, value)
    limit_recovery_ok = all(
        p["first_kind"] == "permanent" and p["first_code"] == "attempt_limit_exhausted"
        and p["stored"]["status"] == "attempt_limit_exhausted"
        and p["requests"] == 3 and p["second_kind"] == "permanent"
        and p["second_code"] == "attempt_limit_exhausted"
        for p in limit_parts.values()
    )

    record(
        "CA-21",
        "首次含在内 ≤3 次 full-jitter 退避 + 重启/普通重试不清零 attempt_no + 无三层重试放大 + 持久 retry_at 新 session 不重复出网 + 失败提交后失联窗口原子持久化 + 终止决定（幂等/非幂等）持久化恢复不重发 + 次数耗尽恢复不重发",
        a_ok and b_ok and no_amp_ok and persist_ok and atomic_loss_ok
        and term_recovery_ok and limit_recovery_ok,
        dict(a_ok=a_ok, outcome_code=outcome.error_code, transport_calls=t.calls,
             attempts=[r["attempt_no"] for r in rows],
             b_ok=b_ok, outcome2_code=outcome2.error_code, transport2_calls=t2.calls,
             no_amp_ok=no_amp_ok,
             persist_ok=persist_ok, first_kind=first.kind, first_backoff=first.backoff_seconds,
             stored_retry_at=str(stored_retry_at), second_kind=second.kind,
             second_calls=second_calls, second_backoff=second.backoff_seconds,
             atomic_loss_ok=atomic_loss_ok, stored_retry_at_e=str(stored_retry_at_e),
             loss_requests=count_e, recovered_kind=recovered_e.kind,
             recovered_backoff=recovered_e.backoff_seconds,
             term_recovery_ok=term_recovery_ok, term_parts=term_parts,
             limit_recovery_ok=limit_recovery_ok, limit_parts=limit_parts),
    )


# ── CA-22 ASR 正常轮询与超时 ──────────────────────────────────────────
async def ca_22(current):
    await _s3_reset()

    # (a) pending → success：正常按间隔轮询，不重提 submit，总期限锚定首次发出
    orig_interval = settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS
    settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS = 0.01
    try:
        a, _ = await create_lecture(SUBJECT_A, "ca22")
        jid = await create_asr_job(a)
        p = FakeASRProvider()
        p.pending_polls = 2
        async with async_session_factory() as db:
            await job_service.run_job(db, jid, provider=p)
        async with async_session_factory() as db:
            j = await db.get(ProcessingJob, jid)
        submit_pc = await _pc_count(f"asr:submit:{jid}")
        poll_pc = await _pc_count(f"asr:poll:{jid}:")
        async with async_session_factory() as db:
            dl = await operation_deadline(db, f"asr:submit:{jid}")
            first = (await db.execute(
                select(func.min(ProviderCall.started_at)).where(
                    ProviderCall.operation_key == f"asr:submit:{jid}"
                )
            )).scalar()
        pending_ok = (
            j.status == "succeeded" and p.submit_calls == 1 and p.poll_calls == 3
            and submit_pc == 1 and poll_pc == 3
            and (dl - first).total_seconds() == settings.CHECKPOINT_EXT_OP_TIMEOUT_SECONDS
        )
    finally:
        settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS = orig_interval

    # (b) 供应商终态失败：不无限轮询（permanent 1 次即止）
    await _s3_reset()
    a2, _ = await create_lecture(SUBJECT_A, "ca22-term")
    jid2 = await create_asr_job(a2)
    p2 = FakeASRProvider()
    p2.poll_result = "incomplete"
    async with async_session_factory() as db:
        await job_service.run_job(db, jid2, provider=p2)
    async with async_session_factory() as db:
        j2 = await db.get(ProcessingJob, jid2)
    term_ok = (
        p2.submit_calls == 1 and p2.poll_calls == 1
        and j2.status == "failed" and j2.error_type == "invalid_result"
    )

    # (c) pending 夹杂查询失败：pending 后一次限流，仍按同一 deadline 恢复成功（CA-22）
    await _s3_reset()
    a3, _ = await create_lecture(SUBJECT_A, "ca22-queryfail")
    jid3 = await create_asr_job(a3)

    class _QueryFailProvider(FakeASRProvider):
        def __init__(self):
            super().__init__()
            self._poll_states = ["pending", "rate_limit"]

        async def poll(self, task_id):
            self.poll_calls += 1
            if self._poll_states:
                s = self._poll_states.pop(0)
                if s == "pending":
                    return None
                raise ProviderError(ERR_RATE_LIMIT, "轮询限流", retryable=True, code="rate_limited")
            return [dict(x) for x in self.segments]

    p3 = _QueryFailProvider()
    orig_interval = settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS
    orig_base = settings.CHECKPOINT_EXT_BACKOFF_BASE
    orig_cap = settings.CHECKPOINT_EXT_BACKOFF_CAP
    settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS = 0.01
    settings.CHECKPOINT_EXT_BACKOFF_BASE = 0.0
    settings.CHECKPOINT_EXT_BACKOFF_CAP = 0.0
    try:
        async with async_session_factory() as db:
            await job_service.run_job(db, jid3, provider=p3)
        async with async_session_factory() as db:
            j3 = await db.get(ProcessingJob, jid3)
        query_fail_ok = j3.status == "succeeded" and p3.submit_calls == 1 and p3.poll_calls == 3
    finally:
        settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS = orig_interval
        settings.CHECKPOINT_EXT_BACKOFF_BASE = orig_base
        settings.CHECKPOINT_EXT_BACKOFF_CAP = orig_cap

    # (d) 持续 pending 至期限耗尽 + 重启不重置期限：回拨首次发出时间，再运行立即期限耗尽，
    #     不产生新 poll（CA-22）。
    await _s3_reset()
    a4, _ = await create_lecture(SUBJECT_A, "ca22-deadline")
    jid4 = await create_asr_job(a4)
    p4 = FakeASRProvider()
    p4.poll_result = "incomplete"  # 提交成功后 poll 终态失败，得到已提交的 job + submit 记录
    async with async_session_factory() as db:
        await job_service.run_job(db, jid4, provider=p4)
    backdated = now() - timedelta(seconds=settings.CHECKPOINT_EXT_OP_TIMEOUT_SECONDS + 1)
    async with async_session_factory() as db:
        await db.execute(update(ProviderCall).where(
            ProviderCall.operation_key == f"asr:submit:{jid4}"
        ).values(started_at=backdated))
        dl4 = await operation_deadline(db, f"asr:submit:{jid4}")
        job4 = await db.get(ProcessingJob, jid4)
        job4.status = "queued"
        job4.error_type = None
        job4.error_code = None
        await db.commit()
    deadline_anchored = (dl4 - backdated).total_seconds() == settings.CHECKPOINT_EXT_OP_TIMEOUT_SECONDS
    p4b = FakeASRProvider()
    p4b.pending_polls = 1_000_000  # 持续 pending
    async with async_session_factory() as db:
        await job_service.run_job(db, jid4, provider=p4b)
    async with async_session_factory() as db:
        j4 = await db.get(ProcessingJob, jid4)
    deadline_exhaust_ok = (
        deadline_anchored and p4b.poll_calls == 0
        and j4.status == "failed" and j4.error_code == "poll_deadline_exhausted"
    )

    # (e) 正常 pending 重启不耗尽错误尝试次数：pending 成功提交后、序列推进前失联，
    #     多轮恢复按最新查询的持久状态推进 seq，不把成功 pending 累加为连续错误、不误触发
    #     attempt_limit_exhausted（CA-22）。隔离预算干扰（轮询计数诊断，非冻结 5/10/10 元预算证据）。
    await _s3_reset()
    a5, _ = await create_lecture(SUBJECT_A, "ca22-restart")
    jid5 = await create_asr_job(a5)
    orig_interval5 = settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS
    settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS = 0.01
    cap_names5 = ("BUDGET_SUBJECT_DAILY_CNY", "BUDGET_GLOBAL_DAILY_CNY", "BUDGET_TRIAL_TOTAL_CNY")
    orig_caps5 = {name: getattr(settings, name) for name in cap_names5}
    for name in cap_names5:
        setattr(settings, name, 1000)
    # 注入：pending 查询结果在 _finish 原子提交后、调用方推进 seq 前立即失联（CA-22 原始缺陷）。
    finish5 = gateway._finish

    async def _lose_after_pending(*args, **kw):
        result = await finish5(*args, **kw)
        if args[1].operation_type == "asr_poll" and result.kind == "success" and result.result is None:
            raise job_service.WorkerLost()
        return result

    gateway._finish = _lose_after_pending
    statuses5 = []
    polls5 = []
    try:
        for _run in range(4):
            if _run:
                await expire_lease(jid5)
            _prov = FakeASRProvider()
            _prov.pending_polls = 100
            try:
                async with async_session_factory() as db:
                    await job_service.run_job(db, jid5, provider=_prov)
            except job_service.WorkerLost:
                pass
            polls5.append(_prov.poll_calls)
            async with async_session_factory() as db:
                _j5 = await db.get(ProcessingJob, jid5)
                statuses5.append(_j5.status)
        rows0 = await _pc_rows(f"asr:poll:{jid5}:0")
        restart_pending_ok = (
            all(s == "running" for s in statuses5)
            and polls5 == [1, 1, 1, 1]
            and len(rows0) == 1 and rows0[0]["attempt_no"] == 1
        )
    finally:
        gateway._finish = finish5
        settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS = orig_interval5
        for name, value in orig_caps5.items():
            setattr(settings, name, value)

    record(
        "CA-22",
        "pending 正常按间隔轮询不重提 submit + 总轮询期限锚定首次发出 + 终态失败不无限轮询 + pending 夹杂查询失败恢复 + 持续 pending 期限耗尽且重启不重置 + pending 成功提交后失联多轮恢复不耗尽错误尝试",
        pending_ok and term_ok and query_fail_ok and deadline_exhaust_ok and restart_pending_ok,
        dict(pending_ok=pending_ok, submit_calls=p.submit_calls, poll_calls=p.poll_calls,
             submit_pc=submit_pc, poll_pc=poll_pc,
             deadline_delta=(dl - first).total_seconds(),
             term_ok=term_ok, term_poll_calls=p2.poll_calls, term_type=j2.error_type,
             query_fail_ok=query_fail_ok, query_fail_polls=p3.poll_calls,
             query_fail_status=j3.status,
             deadline_exhaust_ok=deadline_exhaust_ok, deadline_anchored=deadline_anchored,
             deadline_polls=p4b.poll_calls, deadline_status=j4.status,
             deadline_code=j4.error_code,
             restart_pending_ok=restart_pending_ok, restart_statuses=statuses5,
             restart_polls=polls5,
             seq0_attempts=[r["attempt_no"] for r in rows0]),
    )


# ── CA-23 取消、重试与费用保留 ────────────────────────────────────────
async def ca_23(current):
    await _s3_reset()

    # (a) 等待中取消：不发下一请求、保留已发生消耗（此处无出网，无预算）
    await _occupy_permits(2)
    a, _ = await create_lecture(SUBJECT_A, "ca23-wait")
    jid = await create_asr_job(a)
    p = FakeASRProvider()
    async with async_session_factory() as db:
        await job_service.run_job(db, jid, provider=p)
    async with async_session_factory() as db:
        j = await db.get(ProcessingJob, jid)
        wait_status = j.status
    async with async_session_factory() as db:
        await job_service.cancel_job(db, jid)
    async with async_session_factory() as db:
        jc = await db.get(ProcessingJob, jid)
    cancel_pc = await _pc_count(f"asr:submit:{jid}")
    cancel_ok = (
        wait_status == "retry_wait" and jc.status == "cancelled"
        and p.submit_calls == 0 and cancel_pc == 0
    )

    # (b) 对 unknown 普通重试：不自动重发，保留未知费用
    await _s3_reset()
    a2, _ = await create_lecture(SUBJECT_A, "ca23-unknown")
    jid2 = await create_asr_job(a2)
    p2 = FakeASRProvider()
    p2.submit_unknown = True
    async with async_session_factory() as db:
        await job_service.run_job(db, jid2, provider=p2)
    async with async_session_factory() as db:
        jb = await db.get(ProcessingJob, jid2)
    rows2 = await _pc_rows(f"asr:submit:{jid2}")
    st2 = await _budget_state(SUBJECT_A)
    unknown_ok = (
        jb.status == "failed" and jb.error_type == "provider_unknown"
        and p2.submit_calls == 1 and len(rows2) == 1
        and rows2[0]["status"] == "unknown" and rows2[0]["usage_state"] == "unknown"
        and st2["subject_daily"]["settled_micro"] == rows2[0]["settled_amount"] > 0
    )

    # (c) 耗尽后显式新操作：重新受控计费，旧审计保留
    await _s3_reset()
    t = _AlwaysFailTransport()
    old_key = "ca23:op:old"
    async with async_session_factory() as db:
        spec = CallSpec(operation_key=old_key, operation_type="organize_extract",
                        operation_family=rl.FAMILY_GEN, provider="fake", subject_id=SUBJECT_A)
        await call_controlled(db, spec, t, rng=_ZeroRng())
    old_rows = await _pc_rows(old_key)
    async def _ok():
        return {"ok": True}

    async with async_session_factory() as db:
        spec2 = CallSpec(operation_key="ca23:op:new", operation_type="organize_extract",
                         operation_family=rl.FAMILY_GEN, provider="fake", subject_id=SUBJECT_A)
        outcome2 = await call_controlled(db, spec2, _ok, rng=_ZeroRng())
    new_rows = await _pc_rows("ca23:op:new")
    explicit_ok = (
        len(old_rows) == 3 and outcome2.kind == "success"
        and len(new_rows) == 1 and new_rows[0]["status"] == "succeeded"
    )

    # (d) 取消发生在首次 submit 内并返回可重试错误：不再进入第 2 次 submit，任务取消（CA-23）
    await _s3_reset()
    a3, _ = await create_lecture(SUBJECT_A, "ca23-cancel-during-retry")
    jid3 = await create_asr_job(a3)

    class _CancelOnFirstSubmit(FakeASRProvider):
        def __init__(self):
            super().__init__()
            self.entries = 0

        async def submit(self, local_path):
            self.entries += 1
            if self.entries == 1:
                async with async_session_factory() as db:
                    await job_service.cancel_job(db, jid3)
                raise ProviderError("rate_limited", "confirmed not executed", retryable=True)
            return await super().submit(local_path)

    p3 = _CancelOnFirstSubmit()
    orig_base = settings.CHECKPOINT_EXT_BACKOFF_BASE
    orig_cap = settings.CHECKPOINT_EXT_BACKOFF_CAP
    settings.CHECKPOINT_EXT_BACKOFF_BASE = 0.0
    settings.CHECKPOINT_EXT_BACKOFF_CAP = 0.0
    try:
        async with async_session_factory() as db:
            await job_service.run_job(db, jid3, provider=p3)
    finally:
        settings.CHECKPOINT_EXT_BACKOFF_BASE = orig_base
        settings.CHECKPOINT_EXT_BACKOFF_CAP = orig_cap
    async with async_session_factory() as db:
        jc3 = await db.get(ProcessingJob, jid3)
    rows3 = await _pc_rows(f"asr:submit:{jid3}")
    cancel_during_retry_ok = (
        p3.entries == 1 and jc3.status == "cancelled" and len(rows3) == 1
        and p3.submit_calls == 0
    )

    record(
        "CA-23",
        "等待中取消不发下一请求 + unknown 不自动重发并保留费用 + 耗尽后显式新操作重新受控保留旧审计 + 取消发生在首次 submit 内不再发第 2 次",
        cancel_ok and unknown_ok and explicit_ok and cancel_during_retry_ok,
        dict(cancel_ok=cancel_ok, wait_status=wait_status, cancel_status=jc.status,
             cancel_pc=cancel_pc, submit_calls=p.submit_calls,
             unknown_ok=unknown_ok, unknown_status=jb.status, unknown_settled=st2["subject_daily"]["settled_micro"],
             explicit_ok=explicit_ok, old_attempts=len(old_rows), new_attempts=len(new_rows),
             cancel_during_retry_ok=cancel_during_retry_ok, cancel_entries=p3.entries,
             cancel_submit_calls=p3.submit_calls, cancel_status3=jc3.status,
             cancel_attempts=len(rows3)),
    )


async def _tables_exist(name: str, tables: list[str]) -> bool:
    """给定库中所有表均存在则 True（CA-25 迁移前后结构断言）。"""
    conn = await asyncpg.connect(pg_dsn(name))
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        existing = {r["table_name"] for r in rows}
    finally:
        await conn.close()
    return all(t in existing for t in tables)


# ── CA-24 可观测性与范围隔离 ──────────────────────────────────────────
async def ca_24(current):
    await _s3_reset()
    from app.api.diagnostics import diagnostic_status
    from httpx import AsyncClient

    local_user = settings.LOCAL_SUBJECT_ID

    # (a) 真实 HTTP 上传：可信主体归属由服务端 scope 决定，客户端不可自选（CA-24 修复点）
    l_local, c_local = await create_lecture(local_user, "ca24-local")
    transport = _asgi_transport()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/lectures/{l_local}/audio",
            files={"file": ("sample.wav", _make_wav_bytes(duration_seconds=4.0), "audio/wav")},
        )
    up = resp.json()
    asr_jid = uuid.UUID(up["job_id"]) if up.get("job_id") else None
    async with async_session_factory() as db:
        j_upload = await db.get(ProcessingJob, asr_jid) if asr_jid else None
        job_subject = j_upload.subject_id if j_upload else None
    p_asr = FakeASRProvider()
    async with async_session_factory() as db:
        await job_service.run_job(db, asr_jid, provider=p_asr)
    async with async_session_factory() as db:
        aj = await db.get(ProcessingJob, asr_jid)
        tv_id = aj.output_version
        asr_calls = (await db.execute(
            select(ProviderCall).where(ProviderCall.job_id == asr_jid)
        )).scalars().all()
    upload_ok = (
        resp.status_code == 201 and up.get("job_id") and up.get("asset_id")
        and job_subject == local_user
        and aj.status == "succeeded" and len(asr_calls) >= 2
        and all(r.subject_id == local_user for r in asr_calls)
    )

    # (b) 完整链路 IndexIntent 归属/审计 + 本人请求计数（含无 job 的调用）+ 成功产物 vs 复用（初次执行）
    async with async_session_factory() as db:
        org_job = await organize_service.organize_service.create_organize_job(
            db, ScopeContext(subject_id=local_user), lecture_id=l_local, transcript_version_id=tv_id
        )
        org_jid = org_job.id
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, org_jid, model=FakeOrganizeModel())
    async with async_session_factory() as db:
        intents = (await db.execute(select(IndexIntent))).scalars().all()
        intent_id = intents[0].id if intents else None
    embed = CountingEmbed()
    if intent_id is not None:
        async with async_session_factory() as db:
            intent = await db.get(IndexIntent, intent_id)
            await index_service.index_service.consume_intent(db, intent, embed=embed)
    async with async_session_factory() as db:
        diag_local = await diagnostic_status(db, ScopeContext(subject_id=local_user))
        intent_rows = (await db.execute(
            select(ProviderCall).where(ProviderCall.operation_key.like("index:intent:%"))
        )).scalars().all()
        subject_total = (await db.execute(
            select(func.count(ProviderCall.id)).where(ProviderCall.subject_id == local_user)
        )).scalar_one()
    indexintent_ok = (
        intent_id is not None and len(intent_rows) >= 1
        and all(r.subject_id == local_user and r.job_id is None for r in intent_rows)
        and diag_local["metrics"]["provider_requests"] == subject_total
        and diag_local["metrics"]["provider_requests"] >= len(intent_rows)
    )
    first_run_ok = (
        diag_local["metrics"]["successful_checkpoints"] >= 1
        and diag_local["metrics"]["reuse_count"] == 0
    )

    # (c) 复用：初次执行 vs 真实恢复的统计变化（成功产物存量 vs 实际复用次数）。
    # 汇总 Checkpoint 提交后 worker 失联（CA-04 同款），恢复复用已成功 Checkpoint 而非重新外发；
    # 成功产物（Checkpoint 存量）与复用（reuse_count）是两个独立只增指标。
    l_r, _ = await create_lecture(SUBJECT_A, "ca24-reuse")
    _, v_r = await add_transcript(l_r, make_long_segments(n=2, base=2000))
    j_r = await make_organize_job(l_r, v_r)
    async with async_session_factory() as db:
        d0 = await diagnostic_status(db, ScopeContext(subject_id=SUBJECT_A))
    fake1 = FakeOrganizeModel()
    organize_service._post_summary_hook = _boom_worker_crash  # 汇总 Checkpoint 提交后退出
    crashed = False
    try:
        async with async_session_factory() as db:
            await organize_service.organize_service.run_organize_job(db, j_r, model=fake1)
    except WorkerCrash:
        crashed = True
    finally:
        organize_service._post_summary_hook = None
    await expire_lease(j_r)
    async with async_session_factory() as db:
        d1 = await diagnostic_status(db, ScopeContext(subject_id=SUBJECT_A))
    first_exec_ok = (
        crashed
        and d1["metrics"]["successful_checkpoints"] > d0["metrics"]["successful_checkpoints"]
        and d1["metrics"]["reuse_count"] == d0["metrics"]["reuse_count"]  # 初次执行无复用
    )
    fake2 = FakeOrganizeModel()
    async with async_session_factory() as db:
        await organize_service.organize_service.run_organize_job(db, j_r, model=fake2)
    async with async_session_factory() as db:
        j_r_row = await db.get(ProcessingJob, j_r)
        d2 = await diagnostic_status(db, ScopeContext(subject_id=SUBJECT_A))
    reuse_ok = (
        j_r_row.status == "succeeded"
        and fake2.summarize_calls == 0  # 汇总复用，不重复外发
        and d2["metrics"]["reuse_count"] > d1["metrics"]["reuse_count"]  # 恢复命中已成功 Checkpoint
        and d2["metrics"]["successful_checkpoints"] > d1["metrics"]["successful_checkpoints"]  # 发布新增产物
    )

    # (d) 限流：耗尽用户桶后 organize 入口 429，无新增费用/产物/调用记录
    l_rl, _ = await create_lecture(local_user, "ca24-rl")
    _, v_rl = await add_transcript(l_rl, make_long_segments(n=1, base=200))

    async def _pc_total():
        async with async_session_factory() as db:
            return (await db.execute(select(func.count(ProviderCall.id)))).scalar_one()

    before_pc = await _pc_total()
    before_rl = await _job_outbox_counts("organize", l_rl)
    orig_ucap = settings.RATE_USER_CAPACITY
    orig_uref = settings.RATE_USER_REFILL
    settings.RATE_USER_CAPACITY = 3
    settings.RATE_USER_REFILL = 0.1
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await _exhaust_user_bucket(rl.FAMILY_ORGANIZE)
            r_rl = await client.post(
                f"/api/lectures/{l_rl}/organize",
                json={"transcript_version_id": str(v_rl)},
            )
            rl_body = r_rl.json()
            after_pc = await _pc_total()
            after_rl = await _job_outbox_counts("organize", l_rl)
    finally:
        settings.RATE_USER_CAPACITY = orig_ucap
        settings.RATE_USER_REFILL = orig_uref
    rate_ok = (
        r_rl.status_code == 429 and rl_body.get("code") == "rate_limited"
        and after_pc == before_pc and after_rl == before_rl
    )

    # (e) unknown：结果未知调用 → provider_calls=unknown + unknown_calls 指标 + 归属审计
    a_u, _ = await create_lecture(SUBJECT_A, "ca24-unknown")
    jid_u = await create_asr_job(a_u)
    p_u = FakeASRProvider()
    p_u.submit_unknown = True
    async with async_session_factory() as db:
        await job_service.run_job(db, jid_u, provider=p_u)
    async with async_session_factory() as db:
        j_u = await db.get(ProcessingJob, jid_u)
        diag_u = await diagnostic_status(db, ScopeContext(subject_id=SUBJECT_A))
        u_call = (await db.execute(
            select(ProviderCall).where(ProviderCall.operation_key == f"asr:submit:{jid_u}")
        )).scalars().first()
    unknown_ok = (
        j_u.status == "failed" and j_u.error_type == "provider_unknown"
        and u_call is not None and u_call.status == "unknown" and u_call.subject_id == SUBJECT_A
        and diag_u["metrics"]["unknown_calls"] >= 1
    )

    # (f) 预算拒绝：subject_daily 耗尽 → 新入口不发起外部调用、无新增费用
    await _s3_reset()
    cap_subject = settings.BUDGET_SUBJECT_DAILY_CNY * settings.BUDGET_MICRO_PER_CNY
    await _set_account("subject_daily", SUBJECT_A, settled_micro=cap_subject)
    a_b, _ = await create_lecture(SUBJECT_A, "ca24-budget")
    jid_b = await create_asr_job(a_b)
    p_b = FakeASRProvider()
    async with async_session_factory() as db:
        await job_service.run_job(db, jid_b, provider=p_b)
    async with async_session_factory() as db:
        j_b = await db.get(ProcessingJob, jid_b)
    st_b = await _budget_state(SUBJECT_A)
    budget_ok = (
        p_b.submit_calls == 0 and p_b.poll_calls == 0
        and j_b.status == "failed" and j_b.error_type == "budget_exhausted"
        and st_b["subject_daily"]["settled_micro"] == cap_subject  # 无新增费用
    )

    # (g) B 隔离 + 脱敏扫描
    async with async_session_factory() as db:
        aid_b = uuid.uuid4()
        amt_b = await budget.reserve_budget(
            db, attempt_id=aid_b, operation_key="ca24:b", attempt_no=1, subject_id=SUBJECT_B
        )
        await budget.settle_budget(db, attempt_id=aid_b, settled_micro=amt_b, usage_state="confirmed_sent")
        await db.commit()
    async with async_session_factory() as db:
        diag_a = await diagnostic_status(db, ScopeContext(subject_id=SUBJECT_A))
        diag_b = await diagnostic_status(db, ScopeContext(subject_id=SUBJECT_B))
    st_a = await _budget_state(SUBJECT_A)
    st_bb = await _budget_state(SUBJECT_B)
    isolation_ok = (
        diag_a["subject_id"] == SUBJECT_A and diag_b["subject_id"] == SUBJECT_B
        and diag_a["budget"]["subject_daily"]["settled_micro"] == st_a["subject_daily"]["settled_micro"]
        and diag_b["budget"]["subject_daily"]["settled_micro"] == st_bb["subject_daily"]["settled_micro"]
        and diag_a["budget"]["subject_daily"]["settled_micro"] != st_bb["subject_daily"]["settled_micro"]
        and diag_b["jobs"]["total"] == 0
    )
    payload = json.dumps(diag_a, ensure_ascii=False, default=str).lower()
    secret_scan_ok = all(
        k not in payload
        for k in ("api_key", "secret", "signature", "signed_url", "storage_key",
                  "password", "authorization", "bearer")
    ) and all(k not in diag_a for k in ("content", "quote", "segments"))

    record(
        "CA-24",
        "真实HTTP上传可信主体归属 + IndexIntent无job调用按主体审计/计数 + 成功产物vs实际复用区分(初次/恢复) + 复用/限流/unknown/预算拒绝证据 + B隔离 + 脱敏无密钥/签名URL/原文",
        upload_ok and indexintent_ok and first_run_ok and first_exec_ok and reuse_ok
        and rate_ok and unknown_ok and budget_ok and isolation_ok and secret_scan_ok,
        dict(upload_ok=upload_ok, upload_status=resp.status_code, job_subject=job_subject,
             asr_calls=len(asr_calls),
             asr_subject_ok=all(r.subject_id == local_user for r in asr_calls),
             indexintent_ok=indexintent_ok, intent_rows=len(intent_rows),
             provider_requests=diag_local["metrics"]["provider_requests"],
             successful_checkpoints=diag_local["metrics"]["successful_checkpoints"],
             reuse_count_first=diag_local["metrics"]["reuse_count"],
             first_exec_ok=first_exec_ok,
             cp0=d0["metrics"]["successful_checkpoints"], cp1=d1["metrics"]["successful_checkpoints"],
             cp2=d2["metrics"]["successful_checkpoints"],
             reuse0=d0["metrics"]["reuse_count"], reuse1=d1["metrics"]["reuse_count"],
             reuse2=d2["metrics"]["reuse_count"], reuse_ok=reuse_ok,
             fake2_summarize=fake2.summarize_calls,
             rate_ok=rate_ok, rate_status=r_rl.status_code, rate_pc_delta=after_pc - before_pc,
             unknown_ok=unknown_ok, unknown_calls=diag_u["metrics"]["unknown_calls"],
             budget_ok=budget_ok, budget_settled=st_b["subject_daily"]["settled_micro"],
             isolation_ok=isolation_ok, a_settled=st_a["subject_daily"]["settled_micro"],
             b_settled=st_bb["subject_daily"]["settled_micro"], b_jobs=diag_b["jobs"]["total"],
             secret_scan_ok=secret_scan_ok),
    )


# ── CA-25 干净库/旧库迁移及停写只读回退 ────────────────────────────────
async def ca_25(current):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from httpx import AsyncClient

    legacy_db = PREFIX + "legacy25"
    clean_db = PREFIX + "clean25"
    DBS.append(legacy_db)
    DBS.append(clean_db)

    control_tables = ["processing_jobs", "job_checkpoints", "provider_calls",
                      "budget_accounts", "budget_reservations", "provider_permit_slots"]
    product_tables = ["courses", "meetings", "audio_assets", "transcript_versions",
                      "transcript_segments", "note_versions", "index_versions", "index_blocks"]

    # (a) 干净库升级：head 成功且控制表存在
    await create_db(clean_db)
    run_alembic("head", asyncpg_dsn(clean_db))
    clean_ok = await _tables_exist(clean_db, control_tables)

    # (b) 旧库（前四阶段 schema）带真实 active/历史产品图升级：
    #     Course → Meeting → AudioAsset → TranscriptVersion(active) → TranscriptSegment
    #     → NoteVersion(published) → IndexVersion(active) → IndexBlock，处理任务引用真实产物。
    await create_db(legacy_db)
    run_alembic("c4d5e6f7a8b9", asyncpg_dsn(legacy_db))

    owner = settings.LOCAL_SUBJECT_ID
    legacy_subject = "legacy-subject"
    course_id = str(uuid.uuid4())
    meeting_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    tv_id = str(uuid.uuid4())
    nv_id = str(uuid.uuid4())
    ixv_id = str(uuid.uuid4())
    ids = [str(uuid.uuid4()) for _ in range(5)]

    seg_contents = ["历史片段0：课程开场介绍", "历史片段1：核心概念定义", "历史片段2：本节小结"]
    note_content = {"title": "历史笔记标题", "sections": [{"heading": "要点", "text": "历史笔记正文"}]}
    ix_block_content = "历史索引块正文：核心概念检索片段"
    ix_config = {"model": "deterministic", "dimensions": 1024}
    zero_vec = "[" + ",".join("0.0" for _ in range(1024)) + "]"

    conn = await asyncpg.connect(pg_dsn(legacy_db))
    try:
        await conn.execute(
            "INSERT INTO courses (id, owner_id, title, teacher, term) VALUES ($1, $2, $3, $4, $5)",
            course_id, owner, "历史课程-真实引用", "教师甲", "2026春",
        )
        await conn.execute(
            """
            INSERT INTO meetings
                (id, course_id, title, status, legacy_meeting, lecture_date,
                 active_audio_id, active_transcript_version_id, active_note_version_id)
            VALUES ($1, $2, $3, 'processed', false, '2026-10-01'::date, $4, $5, $6)
            """,
            meeting_id, course_id, "历史课时-真实引用", asset_id, tv_id, nv_id,
        )
        await conn.execute(
            """
            INSERT INTO audio_assets
                (id, lecture_id, storage_key, checksum, mime, duration_ms, size, version, is_demo)
            VALUES ($1, $2, 'legacy/test.wav', 'legacy-checksum', 'audio/wav', 3200, 1234, 1, false)
            """,
            asset_id, meeting_id,
        )
        await conn.execute(
            """
            INSERT INTO transcript_versions
                (id, audio_asset_id, lecture_id, version, provider, provider_task_id, status)
            VALUES ($1, $2, $3, 1, 'fake', 'legacy-task-id', 'active')
            """,
            tv_id, asset_id, meeting_id,
        )
        for i, c in enumerate(seg_contents):
            await conn.execute(
                """
                INSERT INTO transcript_segments
                    (id, version_id, seq_index, speaker, content, start_ms, end_ms)
                VALUES ($1, $2, $3, 't', $4, $5, $6)
                """,
                str(uuid.uuid4()), tv_id, i, c, i * 1000, (i + 1) * 1000 - 100,
            )
        await conn.execute(
            """
            INSERT INTO note_versions
                (id, lecture_id, transcript_version_id, pipeline_version, status, content, published_at)
            VALUES ($1, $2, $3, 'classroom-v1', 'published', $4::jsonb, now())
            """,
            nv_id, meeting_id, tv_id, json.dumps(note_content),
        )
        await conn.execute(
            """
            INSERT INTO index_versions
                (id, course_id, status, config, source_count, block_count, activated_at)
            VALUES ($1, $2, 'active', $3::jsonb, 1, 1, now())
            """,
            ixv_id, course_id, json.dumps(ix_config),
        )
        await conn.execute(
            """
            INSERT INTO index_blocks
                (id, index_version_id, course_id, lecture_id, source_type,
                 source_transcript_version_id, segment_start, segment_end, content, embedding, chunk_index)
            VALUES ($1, $2, $3, $4, 'transcript', $5, 0, 2, $6, $7::vector, 0)
            """,
            str(uuid.uuid4()), ixv_id, course_id, meeting_id, tv_id, ix_block_content, zero_vec,
        )
        # 处理任务引用真实 resource_id / input_version / output_version
        await conn.execute(
            """
            INSERT INTO processing_jobs
                (id, type, resource_id, input_version, config_hash, status, stage, error_type, output_version)
            VALUES
                ($1, 'asr', $2, $3, 'legacy-cfg', 'succeeded', 'publishing', NULL, $4),
                ($5, 'organize', $2, $6, 'legacy-cfg', 'succeeded', 'published', NULL, $7),
                ($8, 'index', $9, $6, 'legacy-cfg', 'running', 'building', NULL, NULL),
                ($10, 'asr', $2, $3, 'legacy-cfg', 'failed', 'created', 'provider_error', NULL),
                ($11, 'asr', $2, $3, 'legacy-cfg', 'queued', 'created', NULL, NULL)
            """,
            ids[0], meeting_id, asset_id, tv_id,
            ids[1], tv_id, nv_id,
            ids[2], course_id,
            ids[3], ids[4],
        )
        # 待消费/重试任务各带 outbox（验证停写期不被投递/重排队）
        await conn.execute(
            """
            INSERT INTO outbox_events (id, business_key, event_type, job_id, payload, status)
            VALUES
                ($1, $2, 'asr_requested', $3, $4::jsonb, 'pending'),
                ($5, $6, 'asr_requested', $7, $8::jsonb, 'pending')
            """,
            str(uuid.uuid4()), f"job:{ids[3]}", ids[3],
            json.dumps({"job_id": ids[3]}),
            str(uuid.uuid4()), f"job:{ids[4]}", ids[4],
            json.dumps({"job_id": ids[4]}),
        )
        pre_total = await conn.fetchval("SELECT count(*) FROM processing_jobs")
        pre_seg_count = await conn.fetchval("SELECT count(*) FROM transcript_segments")
    finally:
        await conn.close()

    def _product_digest(seg_texts, note, block_texts):
        return sha256_hex(json.dumps(
            {"segments": seg_texts, "note": note, "blocks": block_texts},
            sort_keys=True, ensure_ascii=False, default=str,
        ))

    pre_digest = _product_digest(seg_contents, note_content, [ix_block_content])

    # 升级旧库到 head（保留前四阶段真实 active/历史产品，不删表、不回填旧配置）
    run_alembic("head", asyncpg_dsn(legacy_db))

    engine25 = create_async_engine(asyncpg_dsn(legacy_db))
    sm25 = async_sessionmaker(engine25, class_=AsyncSession, expire_on_commit=False)
    async with sm25() as db:
        segs = (
            await db.execute(
                select(TranscriptSegment)
                .where(TranscriptSegment.version_id == uuid.UUID(tv_id))
                .order_by(TranscriptSegment.seq_index)
            )
        ).scalars().all()
        seg_texts = [s.content for s in segs]
        nv = await db.get(NoteVersion, uuid.UUID(nv_id))
        note_data = nv.content if nv is not None else None
        ixv = await db.get(IndexVersion, uuid.UUID(ixv_id))
        blocks = (
            await db.execute(
                select(IndexBlock).where(IndexBlock.index_version_id == uuid.UUID(ixv_id))
            )
        ).scalars().all()
        block_texts = [b.content for b in blocks]

        running = await db.get(ProcessingJob, uuid.UUID(ids[2]))
        hist = await db.get(ProcessingJob, uuid.UUID(ids[0]))
        total = (await db.execute(select(func.count(ProcessingJob.id)))).scalar_one()
        running_status, running_epoch = running.status, running.lease_epoch
        running_resume, running_subject = running.resume_count, running.subject_id
        running_idem = running.idempotency_key
        hist_output = hist.output_version
        hist_snapshot = hist.config_snapshot
    post_digest = _product_digest(seg_texts, note_data, block_texts)

    # 升级后真实产品可读：转写片段 / 笔记正文 / active 索引块内容逐字保留，计数与摘要一致。
    products_preserved = (
        total == pre_total == 5
        and seg_texts == seg_contents and block_texts == [ix_block_content]
        and pre_digest == post_digest
        and nv is not None and nv.status == "published" and note_data == note_content
        and ixv is not None and ixv.status == "active" and ixv.block_count == 1
        and running_status == "running"  # 旧不完整状态不伪造成功
        and running_epoch == 0 and running_resume == 0
        and running_subject is None and running_idem is None  # NULL 身份不碰撞
        and hist_output == uuid.UUID(tv_id)  # 历史输出引用指向真实转写版本
        and hist_snapshot is None  # 旧 legacy 配置不回填
    )

    # 升级后写入真实账本/审计/引用：已结算 2.5 元 + 未知 1 元保守预留 + 历史 Checkpoint 指向真实笔记。
    settled_amount = 2_500_000
    unknown_amount = 1_000_000
    day = budget.shanghai_day()
    cp_id = str(uuid.uuid4())
    conn2 = await asyncpg.connect(pg_dsn(legacy_db))
    try:
        await conn2.execute(
            """
            INSERT INTO budget_accounts (id, kind, account_key, day, cap_micro, reserved_micro, settled_micro)
            VALUES
                ($1, 'subject_daily', $2, $3::date, 5000000, 0, $4),
                ($5, 'trial_total', $2, '1970-01-01'::date, 10000000, 0, 4750000)
            """,
            str(uuid.uuid4()), legacy_subject, day, settled_amount, str(uuid.uuid4()),
        )
        await conn2.execute(
            """
            INSERT INTO provider_calls
                (id, operation_key, attempt_no, job_id, subject_id, provider, model, operation_type, input_hash, status, reserved_amount, settled_amount, usage_state)
            VALUES
                ($1, 'asr:submit:legacy:0', 1, $2, $3, 'fake', NULL, 'asr_submit', 'h1', 'succeeded', $4, $4, 'confirmed_sent'),
                ($5, 'organize:summarize:legacy:1', 1, $6, $3, 'fake', NULL, 'organize_summarize', 'h2', 'unknown', $7, $7, 'unknown')
            """,
            str(uuid.uuid4()), ids[0], legacy_subject, settled_amount,
            str(uuid.uuid4()), ids[1], unknown_amount,
        )
        await conn2.execute(
            """
            INSERT INTO job_checkpoints (id, job_id, step_key, unit_key, status, output_ref, output_hash, schema_version)
            VALUES ($1, $2, 'organize.summary', 'stage', 'succeeded', $3, $4, '1')
            """,
            cp_id, ids[1], nv_id, sha256_hex(nv_id),
        )
        pre_pc = await conn2.fetchval("SELECT count(*) FROM provider_calls")
        pre_acct = await conn2.fetchval(
            "SELECT settled_micro FROM budget_accounts WHERE kind='subject_daily' AND account_key=$1", legacy_subject
        )
        pre_unknown = await conn2.fetchval("SELECT settled_amount FROM provider_calls WHERE status='unknown'")
    finally:
        await conn2.close()

    # (c) 在同一隔离库内停写只读回退：retry 503、worker 周期不领取/不外发/不发布、读入口仍可用。
    #     用依赖覆盖把 HTTP 请求与会话工厂指向 legacy_db，证明同一有历史数据库内的回退演练。
    from app.main import app as fastapi_app  # noqa: PLC0415
    import app.api.deps as api_deps  # noqa: PLC0415
    import app.classroom_worker as cw  # noqa: PLC0415

    async def _legacy_get_db():
        async with sm25() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    orig_worker_sf = cw.async_session_factory
    cw.async_session_factory = sm25
    fastapi_app.dependency_overrides[api_deps.get_db] = _legacy_get_db

    orig_ro = settings.CLASSROOM_READ_ONLY
    settings.CLASSROOM_READ_ONLY = True
    try:
        transport = _asgi_transport()
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r_retry = await client.post(f"/api/jobs/{ids[3]}/retry")
            retry_body = r_retry.json()
            r_read = await client.get(f"/api/lectures/{meeting_id}")

        # 服务层重试同样被停写阻断（不依赖路由单独守卫）
        retry_blocked = False
        async with sm25() as db:
            try:
                await job_service.retry_job(db, uuid.UUID(ids[3]))
            except ServiceError as e:
                retry_blocked = e.code == "read_only"

        # 真实 worker 周期（实际 dispatcher/数据库/网关，仅替换供应商替身）
        cycle = await cw.run_cycle(
            provider=FakeASRProvider(), model=FakeOrganizeModel(),
            embed=DeterministicEmbedding(1024),
        )

        # 三个派发器独立调用也直接退出（不依赖 run_cycle 单点守卫）：已有任务不被领取/外发/发布。
        async with sm25() as db:
            asr_disp = await job_service.recover_and_dispatch(db, provider=FakeASRProvider())
        async with sm25() as db:
            org_disp = await organize_service.organize_service.recover_and_dispatch_organize(
                db, model=FakeOrganizeModel(),
            )
        async with sm25() as db:
            idx_disp = await index_service.index_service.recover_and_dispatch_index(
                db, embed=DeterministicEmbedding(1024),
            )
    finally:
        settings.CLASSROOM_READ_ONLY = orig_ro
        fastapi_app.dependency_overrides.pop(api_deps.get_db, None)
        cw.async_session_factory = orig_worker_sf

    async with sm25() as db:
        failed_job = await db.get(ProcessingJob, uuid.UUID(ids[3]))
        queued_job = await db.get(ProcessingJob, uuid.UUID(ids[4]))
        retry_status = failed_job.status if failed_job is not None else None
        queued_status = queued_job.status if queued_job is not None else None
        queued_output = queued_job.output_version if queued_job is not None else None
        outbox = (
            await db.execute(
                select(OutboxEvent).where(OutboxEvent.job_id == uuid.UUID(ids[4]))
            )
        ).scalars().first()
        outbox_status = outbox.status if outbox is not None else None
    readonly_ok = (
        r_retry.status_code == 503 and retry_body.get("code") == "read_only"
        and retry_status == "failed"  # retry 未重新排队
        and retry_blocked  # 服务层重试同被停写阻断
        and r_read.status_code == 200  # 已有 active 结果仍可读
        and cycle.get("read_only") is True  # worker 周期直接退出，不派发
        and queued_status == "queued"  # queued 任务未被领取
        and queued_output is None  # 未发布新产物
        and outbox_status == "pending"  # outbox 未被投递
        and asr_disp.get("read_only") is True and asr_disp.get("dispatched") == 0
        and org_disp.get("read_only") is True and org_disp.get("dispatched") == 0
        and idx_disp.get("read_only") is True and idx_disp.get("dispatched") == 0
    )

    # 回退只读后：控制表保留 + 真实产品/账本/审计/引用/计数保留（不以删除控制表为通过条件）
    conn3 = await asyncpg.connect(pg_dsn(legacy_db))
    try:
        tables_ok = await _tables_exist(legacy_db, control_tables)
        after_total = await conn3.fetchval("SELECT count(*) FROM processing_jobs")
        after_pc = await conn3.fetchval("SELECT count(*) FROM provider_calls")
        after_acct = await conn3.fetchval(
            "SELECT settled_micro FROM budget_accounts WHERE kind='subject_daily' AND account_key=$1", legacy_subject
        )
        after_unknown = await conn3.fetchval("SELECT settled_amount FROM provider_calls WHERE status='unknown'")
        after_running = await conn3.fetchval("SELECT status FROM processing_jobs WHERE id=$1", ids[2])
        after_ref = await conn3.fetchval("SELECT output_ref FROM job_checkpoints WHERE id=$1", cp_id)
        after_seg_count = await conn3.fetchval("SELECT count(*) FROM transcript_segments")
    finally:
        await conn3.close()
    preserve_ok = (
        tables_ok
        and after_total == 5 and after_pc == 2
        and after_acct == settled_amount and after_unknown == unknown_amount
        and after_running == "running"
        and after_ref == nv_id  # Checkpoint 引用真实笔记版本
        and after_seg_count == pre_seg_count == len(seg_contents)  # 前后计数一致
    )

    await engine25.dispose()

    record(
        "CA-25",
        "干净库 head 升级 + 旧库真实 active/历史产品图（课程→课时→音频→转写→片段→笔记→索引）升级后可读、running 不伪成功/NULL 身份不碰撞/旧配置不回填 + 同一隔离库停写只读回退（retry 503、worker 周期不领取/不外发/不发布、读可用）+ 账本/审计/引用/计数保留",
        clean_ok and products_preserved and readonly_ok and preserve_ok,
        dict(clean_ok=clean_ok, control_tables=control_tables, product_tables=product_tables,
             products_preserved=products_preserved, pre_total=pre_total, total=total,
             pre_seg_count=pre_seg_count, after_seg_count=after_seg_count,
             pre_digest=pre_digest, post_digest=post_digest,
             running_status=running_status, running_epoch=running_epoch,
             running_resume=running_resume, running_subject=running_subject,
             hist_output=str(hist_output), hist_snapshot=hist_snapshot,
             note_status=nv.status if nv is not None else None,
             index_status=ixv.status if ixv is not None else None,
             pre_pc=pre_pc, pre_acct=pre_acct, pre_unknown=pre_unknown,
             readonly_ok=readonly_ok, retry_status_code=r_retry.status_code,
             retry_code=retry_body.get("code"), retry_job_status=retry_status,
             retry_blocked=retry_blocked, read_status=r_read.status_code,
             cycle_read_only=cycle.get("read_only"), queued_status=queued_status,
             queued_output=str(queued_output), outbox_status=outbox_status,
             asr_disp_read_only=asr_disp.get("read_only"), asr_disp_dispatched=asr_disp.get("dispatched"),
             org_disp_read_only=org_disp.get("read_only"), org_disp_dispatched=org_disp.get("dispatched"),
             idx_disp_read_only=idx_disp.get("read_only"), idx_disp_dispatched=idx_disp.get("dispatched"),
             preserve_ok=preserve_ok, tables_ok=tables_ok,
             after_total=after_total, after_pc=after_pc,
             after_acct=after_acct, after_unknown=after_unknown,
             after_running=after_running, after_ref=after_ref),
    )


# ── CA-26 完整链路及改动路径回归 ──────────────────────────────────────
async def ca_26(current):
    await _s3_reset()
    from app.services.retrieval_service import retrieval_service
    from fastapi import HTTPException
    from httpx import AsyncClient

    # (1) A 上传 → ASR：20 段长转写
    a, cid = await create_lecture(SUBJECT_A, "ca26-full")
    jid = await create_asr_job(a)
    async with async_session_factory() as db:
        jj = await db.get(ProcessingJob, jid)
        asset = await db.get(AudioAsset, jj.input_version)
        asset.duration_ms = 200_000
        await db.commit()
    p = FakeASRProvider()
    p.segments = make_long_segments(n=20, base=2000)
    async with async_session_factory() as db:
        await job_service.run_job(db, jid, provider=p)
    async with async_session_factory() as db:
        aj = await db.get(ProcessingJob, jid)
        tv_id = aj.output_version
    asr_ok = aj.status == "succeeded" and tv_id is not None

    # (2) 20 块整理（块 7 一次可恢复失败 → 网关重试只补缺失）→ 汇总 → 发布 → IndexIntent
    async with async_session_factory() as db:
        org_job = await organize_service.organize_service.create_organize_job(
            db, ScopeContext(subject_id=SUBJECT_A), lecture_id=a, transcript_version_id=tv_id
        )
        org_jid = org_job.id
    m = RetryOnceExtractModel(7)  # 块 7 首次可重试失败、之后成功（CA-26 恢复只补缺失）
    orig_base = settings.CHECKPOINT_EXT_BACKOFF_BASE
    orig_cap = settings.CHECKPOINT_EXT_BACKOFF_CAP
    settings.CHECKPOINT_EXT_BACKOFF_BASE = 0.0
    settings.CHECKPOINT_EXT_BACKOFF_CAP = 0.0
    try:
        async with async_session_factory() as db:
            await organize_service.organize_service.run_organize_job(db, org_jid, model=m)
    finally:
        settings.CHECKPOINT_EXT_BACKOFF_BASE = orig_base
        settings.CHECKPOINT_EXT_BACKOFF_CAP = orig_cap
    async with async_session_factory() as db:
        oj = await db.get(ProcessingJob, org_jid)
        note = (await db.execute(
            select(NoteVersion).where(NoteVersion.lecture_id == a)
        )).scalars().first()
        intent = (await db.execute(
            select(IndexIntent).where(IndexIntent.note_version_id == note.id)
        )).scalars().first()
        intent_id = intent.id if intent else None
    extract_pc = await _pc_count(f"organize:extract:{org_jid}:")
    summary_pc = await _pc_count(f"organize:summarize:{org_jid}")
    org_ok = (
        oj.status == "succeeded" and oj.last_successful_stage == "note.publish"
        and len(m.calls) == 20 and m.summarize_calls == 1
        and extract_pc == 21 and summary_pc == 1
        and note is not None and note.status == "published"
        and intent_id is not None
    )

    # (3) 消费 IndexIntent → active 索引（受控 embed）
    embed = CountingEmbed()
    async with async_session_factory() as db:
        intent = await db.get(IndexIntent, intent_id)
        await index_service.index_service.consume_intent(db, intent, embed=embed)
    async with async_session_factory() as db:
        active_iv = (await db.execute(
            select(IndexVersion).where(
                IndexVersion.course_id == cid, IndexVersion.status == "active"
            )
        )).scalars().first()
    index_ok = active_iv is not None and embed.calls >= 1

    # (4) 范围检索 + 回听（证据读取，版本正确）
    async with async_session_factory() as db:
        srch = await retrieval_service.search(
            db, ScopeContext(subject_id=SUBJECT_A, course_id=cid), "K07", top_k=3,
            embed=DeterministicEmbedding(1024),
        )
    search_ok = srch["status"] == "ok" and srch["total"] >= 1 and srch["index_status"] == "current"
    resolve_ok = False
    if srch["results"]:
        r0 = srch["results"][0]
        async with async_session_factory() as db:
            try:
                resolved = await retrieval_service.resolve_source(
                    db, ScopeContext(subject_id=SUBJECT_A, course_id=cid),
                    source_type=r0["source_type"],
                    source_version_id=uuid.UUID(r0["source_version_id"]),
                    block_id=r0.get("block_id"),
                    segment_start=r0.get("segment_start"),
                    segment_end=r0.get("segment_end"),
                    quote=r0.get("quote"),
                )
                resolve_ok = bool(resolved.get("content"))
            except Exception:  # noqa: BLE001
                resolve_ok = False

    # (5) 入口拒绝：降用户桶耗尽后 organize 入口 429，无新增费用/产物
    l_entry, _ = await create_lecture(settings.LOCAL_SUBJECT_ID, "ca26-entry")
    _, v_entry = await add_transcript(l_entry, make_long_segments(n=1, base=200))

    async def _pc_total():
        async with async_session_factory() as db:
            return (await db.execute(select(func.count(ProviderCall.id)))).scalar_one()

    before_pc = await _pc_total()
    before_entry = await _job_outbox_counts("organize", l_entry)
    orig_ucap = settings.RATE_USER_CAPACITY
    orig_uref = settings.RATE_USER_REFILL
    settings.RATE_USER_CAPACITY = 3
    settings.RATE_USER_REFILL = 0.1
    try:
        transport = _asgi_transport()
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await _exhaust_user_bucket(rl.FAMILY_ORGANIZE)
            resp = await client.post(
                f"/api/lectures/{l_entry}/organize",
                json={"transcript_version_id": str(v_entry)},
            )
            body = resp.json()
            after_pc = await _pc_total()
            after_entry = await _job_outbox_counts("organize", l_entry)
    finally:
        settings.RATE_USER_CAPACITY = orig_ucap
        settings.RATE_USER_REFILL = orig_uref
    entry_reject_ok = (
        resp.status_code == 429 and body.get("code") == "rate_limited"
        and after_pc == before_pc and after_entry == before_entry
    )

    # (6) B 越权：HTTP 404 + 检索 A 课程在 B 范围下拒绝
    b_lecture, _ = await create_lecture(SUBJECT_B, "ca26-b")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        rb = await client.get(f"/api/lectures/{b_lecture}")
    http_forbid_ok = rb.status_code == 404
    retr_forbid_ok = False
    try:
        async with async_session_factory() as db:
            await retrieval_service.search(
                db, ScopeContext(subject_id=SUBJECT_B, course_id=cid), "K07",
                top_k=3, embed=DeterministicEmbedding(1024),
            )
    except HTTPException as e:
        retr_forbid_ok = e.status_code == 404
    b_forbid_ok = http_forbid_ok and retr_forbid_ok

    record(
        "CA-26",
        "完整链路 A 上传→ASR→20 块整理(含可恢复失败)→汇总→发布→IndexIntent→索引→检索/回听 + 恢复只补缺失 + 入口拒绝无新增费用 + B 越权 404",
        asr_ok and org_ok and index_ok and search_ok and resolve_ok and entry_reject_ok and b_forbid_ok,
        dict(asr_ok=asr_ok, asr_status=aj.status, tv_id=str(tv_id) if tv_id else None,
             org_ok=org_ok, extract_calls=len(m.calls), extract_pc=extract_pc,
             summary_calls=m.summarize_calls, summary_pc=summary_pc,
             note_status=note.status if note else None, intent_id=str(intent_id) if intent_id else None,
             index_ok=index_ok, embed_calls=embed.calls, active_iv=str(active_iv.id) if active_iv else None,
             search_ok=search_ok, search_status=srch["status"], search_total=srch["total"],
             resolve_ok=resolve_ok, r0_type=r0["source_type"] if srch["results"] else None,
             entry_reject_ok=entry_reject_ok, entry_status=resp.status_code,
             entry_pc_delta=after_pc - before_pc,
             b_forbid_ok=b_forbid_ok, b_http=rb.status_code, b_retr=retr_forbid_ok),
    )


# ── 主流程 ────────────────────────────────────────────────────────────

STAGE_TESTS = {
    "S1": [
        ("CA-01", ca_01),
        ("CA-02", ca_02),
        ("CA-03", ca_03),
        ("CA-04", ca_04),
        ("CA-05", ca_05),
        ("CA-06", ca_06),
        ("CA-08", ca_08),
        ("CA-09", ca_09),
        ("CA-11", ca_11),
    ],
    "S2": [
        ("CA-12", ca_12),
        ("CA-13", ca_13),
        ("CA-14", ca_14),
        ("CA-16", ca_16),
    ],
    "S3": [
        ("CA-07", ca_07),
        ("CA-10", ca_10),
        ("CA-15", ca_15),
        ("CA-17", ca_17),
        ("CA-18", ca_18),
        ("CA-19", ca_19),
        ("CA-20", ca_20),
        ("CA-21", ca_21),
        ("CA-22", ca_22),
        ("CA-23", ca_23),
    ],
    "S4": [
        ("CA-24", ca_24),
        ("CA-25", ca_25),
        ("CA-26", ca_26),
    ],
}


async def main(args) -> int:
    stage = args.stage
    out = Path(args.out or (EVIDENCE_DIR / stage))
    out.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    await create_db(MAIN_DB)
    run_alembic("head", asyncpg_dsn(MAIN_DB))

    # S2：真实 Redis 令牌桶。启用入口/供应商限流并指向隔离测试库（db 15），清空后运行。
    if stage == "S2":
        settings.RATE_LIMIT_ENABLED = True
        settings.REDIS_URL = TEST_REDIS_URL
        os.environ["REDIS_URL"] = TEST_REDIS_URL
        await rl.init_pool()
        pool = await rl._get_pool()
        await pool.flushdb()

    # S3：统一调用网关 + 持久预算/并发 permit。启用网关与真实 Redis（令牌桶仍实际运行），
    # 提高桶容量避免多步骤用例被 S2 已验收的小容量桶误限流（S3 验收焦点是预算/退避/permit）。
    if stage == "S3":
        settings.CALL_GATEWAY_ENABLED = True
        settings.RATE_LIMIT_ENABLED = True
        settings.REDIS_URL = TEST_REDIS_URL
        os.environ["REDIS_URL"] = TEST_REDIS_URL
        settings.RATE_USER_CAPACITY = 1000
        settings.RATE_USER_REFILL = 1000.0
        settings.RATE_ASR_SUBMIT_CAPACITY = 1000
        settings.RATE_ASR_SUBMIT_REFILL = 1000.0
        settings.RATE_ASR_QUERY_CAPACITY = 1000
        settings.RATE_ASR_QUERY_REFILL = 1000.0
        settings.RATE_PROVIDER_GEN_CAPACITY = 1000
        settings.RATE_PROVIDER_GEN_REFILL = 1000.0
        settings.RATE_EMBED_CAPACITY = 1000
        settings.RATE_EMBED_REFILL = 1000.0
        await rl.init_pool()
        pool = await rl._get_pool()
        await pool.flushdb()

    # S4：集成回归与交付收口。与 S3 同启网关/Redis，桶容量抬高避免多步骤链路被误限流；
    # 完整链路（20 块 + 一次可恢复失败 + 索引）需大量预算，抬高冻结预算上限（冻结 5/10/10 元
    # 证据见 S3/CA-18/19）；CA-26 入口拒绝子场景在用例内临时降桶、事后恢复。
    if stage == "S4":
        settings.CALL_GATEWAY_ENABLED = True
        settings.RATE_LIMIT_ENABLED = True
        settings.REDIS_URL = TEST_REDIS_URL
        os.environ["REDIS_URL"] = TEST_REDIS_URL
        settings.RATE_USER_CAPACITY = 1000
        settings.RATE_USER_REFILL = 1000.0
        settings.RATE_ASR_SUBMIT_CAPACITY = 1000
        settings.RATE_ASR_SUBMIT_REFILL = 1000.0
        settings.RATE_ASR_QUERY_CAPACITY = 1000
        settings.RATE_ASR_QUERY_REFILL = 1000.0
        settings.RATE_PROVIDER_GEN_CAPACITY = 1000
        settings.RATE_PROVIDER_GEN_REFILL = 1000.0
        settings.RATE_EMBED_CAPACITY = 1000
        settings.RATE_EMBED_REFILL = 1000.0
        settings.BUDGET_SUBJECT_DAILY_CNY = 100000
        settings.BUDGET_GLOBAL_DAILY_CNY = 100000
        settings.BUDGET_TRIAL_TOTAL_CNY = 100000
        await rl.init_pool()
        pool = await rl._get_pool()
        await pool.flushdb()

    tests = STAGE_TESTS.get(stage, [])
    if args.test:
        tests = [(pid, fn) for pid, fn in tests if pid == args.test]

    current = {"subject_id": SUBJECT_A}
    for pid, fn in tests:
        try:
            await fn(current)
        except Exception as e:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc()
            (out / f"_trace_{pid}.txt").write_text(tb, encoding="utf-8")
            record(pid, "脚本异常", False, {"error": repr(e), "trace": tb})

    failed = [r for r in RESULTS if r["result"] != "通过"]
    print("=" * 70)
    print(f"STAGE {stage}  TOTAL {len(RESULTS)}  PASS {len(RESULTS) - len(failed)}  FAIL {len(failed)}")

    (out / "results.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out / "config.json").write_text(
        json.dumps(
            {
                "lease_seconds": settings.CHECKPOINT_LEASE_SECONDS,
                "heartbeat_seconds": settings.CHECKPOINT_HEARTBEAT_SECONDS,
                "permit_max": settings.CHECKPOINT_PROVIDER_PERMIT_MAX,
                "job_max_attempts": settings.JOB_MAX_ATTEMPTS,
                "job_retry_base_seconds": settings.JOB_RETRY_BASE_SECONDS,
                "organize_context_budget": settings.ORGANIZE_CONTEXT_BUDGET,
                "organize_chunk_overlap": settings.ORGANIZE_CHUNK_OVERLAP,
                "index_chunk_size": settings.INDEX_CHUNK_SIZE,
                "index_embedding_dimensions": settings.INDEX_EMBEDDING_DIMENSIONS,
                "pipeline_version": "classroom-v1",
                "database": MAIN_DB,
                "providers": "FakeASRProvider / FakeOrganizeModel / DeterministicEmbedding",
                "rate_limit_enabled": settings.RATE_LIMIT_ENABLED,
                "redis_url": settings.REDIS_URL,
                "rate_user_capacity": settings.RATE_USER_CAPACITY,
                "rate_user_refill": settings.RATE_USER_REFILL,
                "rate_provider_gen_capacity": settings.RATE_PROVIDER_GEN_CAPACITY,
                "rate_provider_gen_refill": settings.RATE_PROVIDER_GEN_REFILL,
                "rate_embed_capacity": settings.RATE_EMBED_CAPACITY,
                "rate_embed_refill": settings.RATE_EMBED_REFILL,
                "rate_asr_submit_capacity": settings.RATE_ASR_SUBMIT_CAPACITY,
                "rate_asr_submit_refill": settings.RATE_ASR_SUBMIT_REFILL,
                "rate_asr_query_capacity": settings.RATE_ASR_QUERY_CAPACITY,
                "rate_asr_query_refill": settings.RATE_ASR_QUERY_REFILL,
                "redis_timeout_seconds": settings.CHECKPOINT_REDIS_TIMEOUT_SECONDS,
                "redis_wait_seconds": settings.CHECKPOINT_REDIS_WAIT_SECONDS,
                "call_gateway_enabled": settings.CALL_GATEWAY_ENABLED,
                "ext_max_attempts": settings.CHECKPOINT_EXT_MAX_ATTEMPTS,
                "ext_backoff_base": settings.CHECKPOINT_EXT_BACKOFF_BASE,
                "ext_backoff_cap": settings.CHECKPOINT_EXT_BACKOFF_CAP,
                "ext_op_timeout_seconds": settings.CHECKPOINT_EXT_OP_TIMEOUT_SECONDS,
                "permit_ttl_seconds": settings.CHECKPOINT_PROVIDER_PERMIT_TTL_SECONDS,
                "control_wait_seconds": settings.CHECKPOINT_CONTROL_WAIT_SECONDS,
                "asr_poll_interval_seconds": settings.CHECKPOINT_ASR_POLL_INTERVAL_SECONDS,
                "budget_subject_daily_cny": settings.BUDGET_SUBJECT_DAILY_CNY,
                "budget_global_daily_cny": settings.BUDGET_GLOBAL_DAILY_CNY,
                "budget_trial_total_cny": settings.BUDGET_TRIAL_TOTAL_CNY,
                "budget_micro_per_cny": settings.BUDGET_MICRO_PER_CNY,
                "budget_reserve_min_cny": settings.BUDGET_RESERVE_MIN_CNY,
            },
            ensure_ascii=False, indent=2,
        ), encoding="utf-8",
    )

    finished = datetime.now(timezone.utc)
    (out / "run.json").write_text(
        json.dumps(
            {
                "exit_code": 1 if failed else 0,
                "stage": stage,
                "started_utc": started.isoformat(),
                "finished_utc": finished.isoformat(),
                "databases": [asyncpg_dsn(n) for n in DBS],
            }, ensure_ascii=False, indent=2,
        ), encoding="utf-8",
    )

    manifest = {}
    for rel in SOURCE_FILES:
        p = ROOT / rel if not rel.startswith("../") else (ROOT.parent / rel[3:])
        manifest[rel] = sha256(p) if p.exists() else None
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 阶段状态快照（不含正文/密钥）：任务/Checkpoint/产物计数
    async with async_session_factory() as db:
        state = {
            "processing_jobs": (await db.execute(select(func.count(ProcessingJob.id)))).scalar_one(),
            "job_checkpoints": (await db.execute(select(func.count(JobCheckpoint.id)))).scalar_one(),
            "succeeded_checkpoints": (await db.execute(
                select(func.count(JobCheckpoint.id)).where(JobCheckpoint.status == "succeeded")
            )).scalar_one(),
            "notes": (await db.execute(select(func.count(NoteVersion.id)))).scalar_one(),
            "index_intents": (await db.execute(select(func.count(IndexIntent.id)))).scalar_one(),
            "provider_calls": (await db.execute(select(func.count(ProviderCall.id)))).scalar_one(),
            "budget_reservations": (await db.execute(select(func.count(BudgetReservation.id)))).scalar_one(),
        }
    (out / "state-before-after.json").write_text(
        json.dumps({"after": state, "note": f"{stage} 阶段末态；逐项前后快照见 results.json detail"},
                   ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if stage in ("S2", "S3", "S4"):
        await rl.close_pool()
    for name in DBS:
        await drop_db(name)
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="S1", choices=["S1", "S2", "S3", "S4"])
    ap.add_argument("--test", default=None, help="仅复验单个 ID，如 CA-01")
    ap.add_argument("--out", default=None, help=f"证据目录，默认 {EVIDENCE_DIR}/<stage>")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args)))
