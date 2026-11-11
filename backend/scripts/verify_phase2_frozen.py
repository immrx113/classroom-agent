"""Phase 02「可靠输入」冻结验收：P02-01～P02-16（P0）独立验证。

针对 docs/classroom-agent/acceptance/phase-02-frozen-checklist-v1.md。
P1（P02-17/18/19）转人工，不在本脚本自动验证。

运行（backend 目录）：
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  python scripts/verify_phase2_frozen.py
"""

import asyncio
import hashlib
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.update(
    OPENAI_API_KEY="", DASHSCOPE_API_KEY="", PYTHONPATH=".", PYTHONUTF8="1"
)

import asyncpg
from app.config import settings

BASE = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
PREFIX = "yuan_phase2_test_" + uuid.uuid4().hex[:10] + "_"
MAIN_DB = PREFIX + "main"

# 在导入会创建引擎的模块前，把引擎指向隔离测试库，绝不触碰生产库 yuan_meet。
settings.DATABASE_URL = BASE.replace("postgresql://", "postgresql+asyncpg://") + "/" + MAIN_DB
os.environ["DATABASE_URL"] = settings.DATABASE_URL

import app.services.audio_asset_service as aas_module
from app.api.deps import get_scope
from app.db.session import async_session_factory
from app.main import app
from app.models.audio_asset import AudioAsset
from app.models.course import Course
from app.models.meeting import Meeting
from app.models.processing_job import ProcessingJob, OutboxEvent
from app.models.transcript_version import TranscriptSegment, TranscriptVersion
from app.services.asr_provider import (
    DemoASRProvider,
    FakeASRProvider,
    clear_provider_override,
    set_provider,
)
from app.services.audio_asset_service import audio_asset_service
from app.services.errors import ServiceError
from app.services.job_service import recover_and_dispatch, run_job
from app.services.scope import ScopeContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]  # backend/（alembic 运行目录）
OUT = ROOT.parent / "docs" / "classroom-agent" / "acceptance" / "evidence-phase2-20261028"
OUT.mkdir(parents=True, exist_ok=True)

SUBJECT_A = "subject-a"
SUBJECT_B = "subject-b"
DBS: list[str] = [MAIN_DB]
RESULTS: list[dict] = []

# 上传目录隔离到本测试专属临时目录，避免污染开发环境
TEST_UPLOAD_DIR = ROOT / "uploads_phase2_test" / uuid.uuid4().hex[:8]
settings.UPLOAD_DIR = str(TEST_UPLOAD_DIR)


def record(pid: str, check: str, ok: bool, detail) -> None:
    row = dict(id=pid, check=check, result="通过" if ok else "不通过", detail=detail)
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


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
    """在独立子进程运行 alembic（env.py 内部会 asyncio.run，不能嵌套在事件循环里）。"""
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


def make_wav(duration_s: float, freq: float = 440.0, sr: int = 16000) -> bytes:
    n = int(sr * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / sr))) for i in range(n)
        )
        w.writeframes(frames)
    return buf.getvalue()


class _BytesStream:
    """把 bytes 包装为按块读取的流，记录读取尺寸。"""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        if self._pos >= len(self._data):
            return b""
        n = min(size, len(self._data) - self._pos)
        self.read_sizes.append(n)
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk


async def upload_bytes(db, lecture_id, filename: str, data: bytes):
    return await audio_asset_service.upload(
        db, lecture_id=lecture_id, filename=filename, file=_BytesStream(data)
    )


async def create_lecture(owner_id: str, title: str) -> tuple[uuid.UUID, uuid.UUID]:
    """创建归属主体为 owner_id 的课程+课时，返回 (lecture_id, course_id)。"""
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


async def set_archived(course_id: uuid.UUID, archived: bool) -> None:
    async with async_session_factory() as db:
        c = await db.get(Course, course_id)
        c.archived_at = now() if archived else None
        await db.commit()


async def main() -> None:
    await create_db(MAIN_DB)
    run_alembic("head", asyncpg_dsn(MAIN_DB))  # 空库 → head（P02-15 路径 1）

    current = {"subject_id": SUBJECT_A}

    async def override_scope():
        return ScopeContext(subject_id=current["subject_id"])

    app.dependency_overrides[get_scope] = override_scope
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for pid, coro in tests(current, client):
                try:
                    await coro
                except Exception as e:  # noqa: BLE001
                    import traceback
                    tb = traceback.format_exc()
                    (OUT / f"_trace_{pid}.txt").write_text(tb, encoding="utf-8")
                    record(pid, "脚本异常", False, {"error": repr(e), "trace": tb})
    finally:
        app.dependency_overrides.clear()

    failed = [r for r in RESULTS if r["result"] != "通过"]
    print("=" * 70)
    print(f"TOTAL {len(RESULTS)}  PASS {len(RESULTS) - len(failed)}  FAIL {len(failed)}")
    (OUT / "results.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "config.json").write_text(
        json.dumps(
            {
                "S": settings.MEDIA_UPLOAD_MAX_BYTES,
                "C": settings.MEDIA_UPLOAD_CHUNK_BYTES,
                "T": settings.JOB_HEARTBEAT_TIMEOUT_SECONDS,
                "R": settings.JOB_MAX_ATTEMPTS,
                "min_duration_ms": settings.MEDIA_MIN_DURATION_MS,
                "max_duration_ms": settings.MEDIA_MAX_DURATION_MS,
                "asr_mode": settings.ASR_MODE,
                "upload_dir": settings.UPLOAD_DIR,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    for name in DBS:
        await drop_db(name)
    # 清理本脚本生成的隔离上传目录（含父目录），避免污染开发环境
    upload_parent = Path(settings.UPLOAD_DIR).parent
    if upload_parent.exists():
        shutil.rmtree(upload_parent, ignore_errors=True)
    if failed:
        sys.exit(1)


def tests(current, client):
    return [
        ("P02-01", p02_01(current, client)),
        ("P02-02", p02_02(current, client)),
        ("P02-03", p02_03(current, client)),
        ("P02-04", p02_04(current, client)),
        ("P02-05", p02_05(current, client)),
        ("P02-06", p02_06(current, client)),
        ("P02-07", p02_07(current, client)),
        ("P02-08", p02_08(current, client)),
        ("P02-09", p02_09(current, client)),
        ("P02-10", p02_10(current, client)),
        ("P02-11", p02_11(current, client)),
        ("P02-12", p02_12(current, client)),
        ("P02-13", p02_13(current, client)),
        ("P02-14", p02_14(current, client)),
        ("P02-15", p02_15()),
        ("P02-16", p02_16(current, client)),
    ]


# ── P02-01 主体/课程边界 + 旧入口停用 ─────────────────────────────────
async def p02_01(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    a1, a_course = await create_lecture(SUBJECT_A, "p0201-a")
    wav = make_wav(2.0)

    current["subject_id"] = SUBJECT_A
    r = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("ok.wav", wav, "audio/wav")})
    ok_upload = r.status_code == 201 and {"asset_id", "job_id"} <= set(r.json())
    asset_id = r.json().get("asset_id")

    current["subject_id"] = SUBJECT_B
    r_b = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("ok.wav", wav, "audio/wav")})
    r_b_get = await client.get(f"/api/audio-assets/{asset_id}/content")

    # 旧入口（未归档、主体 A 可访问）→ 410
    current["subject_id"] = SUBJECT_A
    r_old = await client.post(f"/api/meetings/{a1}/upload")

    # 归档课程后普通读取拒绝
    await set_archived(a_course, True)
    r_arch = await client.get(f"/api/audio-assets/{asset_id}/content")

    record(
        "P02-01",
        "上传/读取/任务继承主体课程边界 + 旧入口 410",
        ok_upload and r_b.status_code == 404 and r_b_get.status_code == 404
        and r_old.status_code == 410 and r_arch.status_code == 404,
        dict(upload=r.status_code, foreign_upload=r_b.status_code,
             foreign_read=r_b_get.status_code, old_entry=r_old.status_code,
             archived_read=r_arch.status_code),
    )


# ── P02-02 危险文件名拒绝 + 服务端存储键 + 同名不覆盖 ──────────────────
def _rejects_name(name: str) -> bool:
    try:
        aas_module._validate_filename(name)
        return False
    except ServiceError:
        return True


async def p02_02(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    a1, _ = await create_lecture(SUBJECT_A, "p0202-a")
    a2, _ = await create_lecture(SUBJECT_A, "p0202-b")
    wav = make_wav(2.0, freq=440.0)
    wav2 = make_wav(2.0, freq=880.0)
    current["subject_id"] = SUBJECT_A

    # 服务层危险文件名直接拒绝（不依赖 HTTP 层对反斜杠/盘符的透传）
    service_rejects = (
        _rejects_name("../evil.wav")
        and _rejects_name("C:\\abs\\evil.wav")
        and _rejects_name("C:/abs/evil.wav")
        and _rejects_name("/etc/passwd.wav")
        and not _rejects_name("same.wav")
    )

    # HTTP 层：父目录文件名 422；同名不同内容不同课时各自独立资产
    r_dotdot = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("../evil.wav", wav, "audio/wav")})
    r1 = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("same.wav", wav, "audio/wav")})
    r2 = await client.post(f"/api/lectures/{a2}/audio", files={"file": ("same.wav", wav2, "audio/wav")})

    async with async_session_factory() as s:
        a1_asset = await s.get(AudioAsset, uuid.UUID(r1.json()["asset_id"]))
        a2_asset = await s.get(AudioAsset, uuid.UUID(r2.json()["asset_id"]))
        key_safe = (a1_asset.storage_key.startswith("assets/")
                    and "same.wav" not in a1_asset.storage_key
                    and "../" not in a1_asset.storage_key)
        no_overwrite = a1_asset.checksum != a2_asset.checksum and a1_asset.storage_key != a2_asset.storage_key

    record(
        "P02-02",
        "路径分隔/父目录/盘符拒绝 + 服务端键 + 同名不覆盖",
        service_rejects and r_dotdot.status_code == 422
        and r1.status_code == 201 and r2.status_code == 201
        and key_safe and no_overwrite,
        dict(service_rejects=service_rejects, dotdot=r_dotdot.status_code,
             key=a1_asset.storage_key, checksums_differ=no_overwrite),
    )


# ── P02-03 分块读取 + 大小上限 ──────────────────────────────────────
async def p02_03(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    a1, _ = await create_lecture(SUBJECT_A, "p0203-a")
    S = settings.MEDIA_UPLOAD_MAX_BYTES
    C = settings.MEDIA_UPLOAD_CHUNK_BYTES
    total = S + 3 * C

    class _FakeStream:
        def __init__(self):
            self.served = 0
            self.read_sizes: list[int] = []

        async def read(self, size: int) -> bytes:
            if self.served >= total:
                return b""
            n = min(size, total - self.served)
            self.read_sizes.append(n)
            self.served += n
            return b"\x00" * n

    stream = _FakeStream()
    try:
        async with async_session_factory() as s:
            await audio_asset_service.upload(s, lecture_id=a1, filename="big.wav", file=stream)
        rejected = False
    except ServiceError as e:
        rejected = e.status_code == 413 and e.code == "upload_too_large"

    async with async_session_factory() as s:
        assets = await s.execute(select(func.count(AudioAsset.id)).where(AudioAsset.lecture_id == a1))
        jobs = await s.execute(select(func.count(ProcessingJob.id)).where(ProcessingJob.resource_id == a1))
        no_side_effect = assets.scalar_one() == 0 and jobs.scalar_one() == 0

    # 正常上传：分块读取，不整文件 read-all
    bs = _BytesStream(make_wav(1.0))
    async with async_session_factory() as s:
        await audio_asset_service.upload(s, lecture_id=a1, filename="normal.wav", file=bs)

    record(
        "P02-03",
        "分块读取 + 累计超限即拒绝且不产生资产/任务",
        rejected and stream.served < total and all(sz <= C for sz in stream.read_sizes)
        and no_side_effect and all(sz <= C for sz in bs.read_sizes),
        dict(S=S, C=C, served=stream.served, total=total,
             read_sizes=stream.read_sizes[:16], normal_reads=bs.read_sizes),
    )


# ── P02-04 真实媒体校验 ─────────────────────────────────────────────
async def p02_04(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    a1, _ = await create_lecture(SUBJECT_A, "p0204-a")
    current["subject_id"] = SUBJECT_A
    wav = make_wav(2.0, freq=330.0)
    fake = b"this is not audio, just plain text" * 200
    corrupt = wav[:20]
    too_short = make_wav(0.1)

    r_ok = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("real.wav", wav, "audio/wav")})
    r_fake = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("fake.wav", fake, "audio/wav")})
    r_corrupt = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("bad.wav", corrupt, "audio/wav")})
    r_short = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("short.wav", too_short, "audio/wav")})

    async with async_session_factory() as s:
        asset = await s.get(AudioAsset, uuid.UUID(r_ok.json()["asset_id"]))
        checksum_ok = asset.checksum == hashlib.sha256(wav).hexdigest()
        mime_ok = asset.mime == "audio/wav"
        dur_ok = asset.duration_ms == 2000

    record(
        "P02-04",
        "真实容器/编码/可解码/时长校验 + 伪造/损坏/过短拒绝",
        r_ok.status_code == 201 and r_fake.status_code == 422 and r_corrupt.status_code == 422
        and r_short.status_code == 422 and checksum_ok and mime_ok and dur_ok,
        dict(ok=r_ok.status_code, fake=r_fake.status_code, corrupt=r_corrupt.status_code,
             short=r_short.status_code, mime=asset.mime, duration_ms=asset.duration_ms),
    )


# ── P02-05 临时/完整资产分离 + 中断 ─────────────────────────────────
async def p02_05(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    a1, _ = await create_lecture(SUBJECT_A, "p0205-a")

    class _BrokenStream:
        def __init__(self):
            self.calls = 0

        async def read(self, size: int) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return make_wav(2.0)[:1000]
            raise ConnectionError("client aborted")

    try:
        async with async_session_factory() as s:
            await audio_asset_service.upload(s, lecture_id=a1, filename="broken.wav", file=_BrokenStream())
        interrupted = False
    except ConnectionError:
        interrupted = True

    async with async_session_factory() as s:
        assets = await s.execute(select(func.count(AudioAsset.id)).where(AudioAsset.lecture_id == a1))
        jobs = await s.execute(select(func.count(ProcessingJob.id)).where(ProcessingJob.resource_id == a1))
        assets_count = assets.scalar_one()
        jobs_count = jobs.scalar_one()
        no_asset = assets_count == 0
        no_job = jobs_count == 0

    tmp_dir = Path(settings.UPLOAD_DIR) / "tmp"
    parts = list(tmp_dir.rglob("*.part")) if tmp_dir.exists() else []

    record(
        "P02-05",
        "上传中断不产生完整资产/任务，临时文件清理",
        interrupted and no_asset and no_job and len(parts) == 0,
        dict(interrupted=interrupted, assets=assets_count, jobs=jobs_count, parts=len(parts)),
    )


# ── P02-06 事务 + outbox 一致性 ─────────────────────────────────────
async def p02_06(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    a1, _ = await create_lecture(SUBJECT_A, "p0206-a")
    current["subject_id"] = SUBJECT_A

    # (a) 注入回滚：flush 后、commit 前抛异常
    def _boom():
        raise RuntimeError("injected rollback")

    aas_module._after_flush_hook = _boom
    try:
        await client.post(f"/api/lectures/{a1}/audio", files={"file": ("rollback.wav", make_wav(2.0), "audio/wav")})
    except Exception:
        pass
    finally:
        aas_module._after_flush_hook = None

    async with async_session_factory() as s:
        assets = await s.execute(select(func.count(AudioAsset.id)).where(AudioAsset.lecture_id == a1))
        jobs = await s.execute(select(func.count(ProcessingJob.id)).where(ProcessingJob.resource_id == a1))
        no_committed = assets.scalar_one() == 0 and jobs.scalar_one() == 0

    # (b) 正常上传：响应含 asset_id + job_id，outbox pending
    r = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("ok.wav", make_wav(2.0), "audio/wav")})
    has_ids = r.status_code == 201 and {"asset_id", "job_id"} <= set(r.json())
    job_id = uuid.UUID(r.json()["job_id"])
    async with async_session_factory() as s:
        job = await s.get(ProcessingJob, job_id)
        outbox = (await s.execute(select(OutboxEvent).where(OutboxEvent.job_id == job_id))).scalars().first()
        job_queued = job.status == "queued"
        outbox_pending = outbox is not None and outbox.status == "pending"

    # (c) 派发后 outbox 可被投递
    async with async_session_factory() as s:
        await recover_and_dispatch(s)

    record(
        "P02-06",
        "回滚无已提交任务 + outbox pending + 响应含 asset/job + 可派发",
        no_committed and has_ids and job_queued and outbox_pending,
        dict(rollback_committed=not no_committed, asset_job_ids=has_ids,
             job_status=job.status if job else None, outbox=outbox.status if outbox else None),
    )


# ── P02-07 固定输入 + 上传重试/重复投递幂等 + B 不暴露 A 资产 ─────────
async def p02_07(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    set_provider(FakeASRProvider())
    a1, _ = await create_lecture(SUBJECT_A, "p0207-a")
    b1, _ = await create_lecture(SUBJECT_B, "p0207-b")
    current["subject_id"] = SUBJECT_A
    wav = make_wav(4.0, freq=440.0)
    wav2 = make_wav(4.0, freq=880.0)

    # 1. 同一已接受上传请求重试（同主体/课时/文件名/字节）→ 返回同一 asset/job
    r1 = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("same.wav", wav, "audio/wav")})
    r2 = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("same.wav", wav, "audio/wav")})
    retry_same_ids = r1.status_code == 201 and r2.status_code in (200, 201) and r1.json() == r2.json()
    asset_id = uuid.UUID(r1.json()["asset_id"])
    job_id = uuid.UUID(r1.json()["job_id"])

    # 2. 同名不同内容 F2 作为明确新输入 → 新资产，不覆盖 F
    r_f2 = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("same.wav", wav2, "audio/wav")})
    f2_new = r_f2.status_code == 201 and r_f2.json()["asset_id"] != asset_id

    # 3. B 独立提交同内容 F → B 自己的资产，不与 A 合并
    current["subject_id"] = SUBJECT_B
    r_b_upload = await client.post(f"/api/lectures/{b1}/audio", files={"file": ("same.wav", wav, "audio/wav")})
    b_separate = r_b_upload.status_code == 201 and r_b_upload.json()["asset_id"] != asset_id
    current["subject_id"] = SUBJECT_A

    async with async_session_factory() as s:
        await recover_and_dispatch(s)

    async with async_session_factory() as s:
        versions = (await s.execute(select(TranscriptVersion).where(TranscriptVersion.audio_asset_id == asset_id))).scalars().all()
        seg_count = (await s.execute(select(func.count(TranscriptSegment.id)).where(TranscriptSegment.version_id == versions[0].id))).scalar_one() if versions else 0
        a_assets = (await s.execute(select(func.count(AudioAsset.id)).where(AudioAsset.lecture_id == a1))).scalar_one()

    # 4. 重复投递：不产生第二份版本
    async with async_session_factory() as s:
        await recover_and_dispatch(s)

    async with async_session_factory() as s:
        v_after = (await s.execute(select(func.count(TranscriptVersion.id)).where(TranscriptVersion.audio_asset_id == asset_id))).scalar_one()
        job = await s.get(ProcessingJob, job_id)
        one_version = v_after == 1 and job.output_version is not None

    current["subject_id"] = SUBJECT_B
    r_b = await client.get(f"/api/audio-assets/{asset_id}/content")
    current["subject_id"] = SUBJECT_A

    record(
        "P02-07",
        "上传重试同 ID + F2 明确新输入 + B 独立同内容隔离 + 重复投递单一版本",
        retry_same_ids and f2_new and b_separate and bool(versions) and seg_count > 0
        and one_version and a_assets == 2 and r_b.status_code == 404,
        dict(retry_ids_same=retry_same_ids, f2_asset=str(r_f2.json()["asset_id"]),
             b_asset=str(r_b_upload.json()["asset_id"]), a_assets=a_assets,
             segments=seg_count, versions_after_redispatch=v_after,
             output_version=str(job.output_version) if job and job.output_version else None,
             foreign_asset_read=r_b.status_code),
    )
    clear_provider_override()


# ── P02-08 授权播放 + Range ─────────────────────────────────────────
async def p02_08(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    a1, a_course = await create_lecture(SUBJECT_A, "p0208-a")
    current["subject_id"] = SUBJECT_A
    wav = make_wav(2.0, freq=500.0)
    r = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("play.wav", wav, "audio/wav")})
    asset_id = r.json()["asset_id"]

    full = await client.get(f"/api/audio-assets/{asset_id}/content")
    full_ok = full.status_code == 200 and full.content == wav and full.headers.get("content-type", "").startswith("audio/wav")

    part = await client.get(f"/api/audio-assets/{asset_id}/content", headers={"Range": "bytes=100-199"})
    part_ok = (part.status_code == 206 and part.content == wav[100:200]
               and part.headers.get("content-range") == f"bytes 100-199/{len(wav)}")

    current["subject_id"] = SUBJECT_B
    r_b = await client.get(f"/api/audio-assets/{asset_id}/content")
    current["subject_id"] = SUBJECT_A
    await set_archived(a_course, True)
    r_arch = await client.get(f"/api/audio-assets/{asset_id}/content")

    record(
        "P02-08",
        "MIME 正确 + Range 中段读取 + 越权/归档拒绝",
        full_ok and part_ok and r_b.status_code == 404 and r_arch.status_code == 404,
        dict(full=full.status_code, range=part.status_code, content_range=part.headers.get("content-range"),
             foreign=r_b.status_code, archived=r_arch.status_code),
    )


# ── P02-09 真实失败不回退 Mock + 演示标记隔离 ────────────────────────
async def p02_09(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    a1, _ = await create_lecture(SUBJECT_A, "p0209-a")
    current["subject_id"] = SUBJECT_A

    r = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("noconf.wav", make_wav(2.0), "audio/wav")})
    job_id = uuid.UUID(r.json()["job_id"])
    asset_id = uuid.UUID(r.json()["asset_id"])
    async with async_session_factory() as s:
        await recover_and_dispatch(s)
    async with async_session_factory() as s:
        job = await s.get(ProcessingJob, job_id)
        versions = await s.execute(select(func.count(TranscriptVersion.id)).where(TranscriptVersion.audio_asset_id == asset_id))
        version_count = versions.scalar_one()
        no_publish = version_count == 0
        config_fail = job.status == "failed" and job.error_type == "config_error"

    # 显式 demo provider：产物标记 is_demo
    set_provider(DemoASRProvider())
    r_demo = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("demo.wav", make_wav(2.0, freq=560.0), "audio/wav")})
    async with async_session_factory() as s:
        asset = await s.get(AudioAsset, uuid.UUID(r_demo.json()["asset_id"]))
        demo_marked = asset.is_demo is True

    record(
        "P02-09",
        "真实配置缺失明确失败不降级 + 演示标记隔离",
        config_fail and no_publish and demo_marked,
        dict(job_status=job.status, error_type=job.error_type, versions=version_count,
             demo_marked=demo_marked),
    )
    clear_provider_override()


# ── P02-10 task_id 持久化 + 恢复沿用同一任务 ────────────────────────
async def p02_10(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    fake = FakeASRProvider()
    set_provider(fake)
    a1, _ = await create_lecture(SUBJECT_A, "p0210-a")
    current["subject_id"] = SUBJECT_A
    wav = make_wav(4.0)

    async with async_session_factory() as s:
        _, job = await upload_bytes(s, a1, "resume.wav", wav)
        job_id = job.id

    # 模拟：worker 提交成功并保存 task_id 后、轮询前退出
    async with async_session_factory() as s:
        j = await s.get(ProcessingJob, job_id)
        local_path = str(Path(settings.UPLOAD_DIR) / (await s.get(AudioAsset, job.input_version)).storage_key)
        submit = await fake.submit(local_path)
        saved_task_id = submit.task_id
        j.provider_task_id = saved_task_id
        j.stage = "submitted"
        j.status = "running"
        j.attempt = 1
        j.heartbeat_at = now()
        await s.commit()

    # 恢复：沿用同一 task_id，不重复提交
    fake.submit_calls = 0
    async with async_session_factory() as s:
        await run_job(s, job_id, provider=fake)
    async with async_session_factory() as s:
        j = await s.get(ProcessingJob, job_id)
        reused_task = fake.submit_calls == 0 and j.provider_task_id == saved_task_id
        succeeded = j.status == "succeeded" and j.output_version is not None

    record(
        "P02-10",
        "task_id 持久化 + 恢复沿用同一任务不重复提交",
        saved_task_id is not None and reused_task and succeeded,
        dict(saved_task_id=saved_task_id, submit_calls_on_resume=fake.submit_calls,
             status=j.status, poll_calls=fake.poll_calls),
    )
    clear_provider_override()


# ── P02-11 结果校验 + 条件发布（旧输入晚完成不覆盖）────────────────────
async def p02_11(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    fake = FakeASRProvider()
    set_provider(fake)
    a1, _ = await create_lecture(SUBJECT_A, "p0211-a")
    current["subject_id"] = SUBJECT_A

    # 1. 倒置时间 → 非法结果不发布
    fake.segments = [{"seq_index": 0, "speaker": "a", "content": "正文", "start_ms": 2000, "end_ms": 1000}]
    async with async_session_factory() as s:
        _, bad_job = await upload_bytes(s, a1, "bad.wav", make_wav(2.0, freq=620.0))
        bad_job_id = bad_job.id
        await run_job(s, bad_job_id, provider=fake)
    async with async_session_factory() as s:
        bad = await s.get(ProcessingJob, bad_job_id)
        invalid_rejected = bad.status == "failed" and bad.error_type == "invalid_result"

    # 2. 新输入先完成并发布，旧输入晚完成不覆盖
    fake.segments = [{"seq_index": 0, "speaker": "a", "content": "旧输入片段", "start_ms": 0, "end_ms": 1000}]
    async with async_session_factory() as s:
        _, old_job = await upload_bytes(s, a1, "old.wav", make_wav(2.0, freq=640.0))
        old_job_id = old_job.id
    fake.segments = [{"seq_index": 0, "speaker": "a", "content": "新输入片段", "start_ms": 0, "end_ms": 1500}]
    async with async_session_factory() as s:
        asset_new, new_job = await upload_bytes(s, a1, "new.wav", make_wav(2.0, freq=660.0))
        new_job_id = new_job.id
        await run_job(s, new_job_id, provider=fake)  # 新输入先完成
    fake.segments = [{"seq_index": 0, "speaker": "a", "content": "旧输入片段", "start_ms": 0, "end_ms": 1000}]
    async with async_session_factory() as s:
        await run_job(s, old_job_id, provider=fake)  # 旧输入晚完成

    async with async_session_factory() as s:
        m = await s.get(Meeting, a1)
        old_job = await s.get(ProcessingJob, old_job_id)
        old_version = await s.get(TranscriptVersion, old_job.output_version)
        old_not_override = m.active_audio_id == asset_new.id
        old_ready = old_version.status == "ready"

    record(
        "P02-11",
        "非法结果不发布 + 旧输入晚完成不覆盖新输入",
        invalid_rejected and old_not_override and old_ready,
        dict(invalid_status=bad.status, active_audio=str(asset_new.id),
             old_version_status=old_version.status),
    )
    clear_provider_override()


# ── P02-12 任务查询 + 失败分型 ──────────────────────────────────────
async def p02_12(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    fake = FakeASRProvider()
    set_provider(fake)
    a1, _ = await create_lecture(SUBJECT_A, "p0212-a")
    current["subject_id"] = SUBJECT_A

    fake.poll_result = "silence"
    async with async_session_factory() as s:
        _, job_silence = await upload_bytes(s, a1, "silence.wav", make_wav(2.0, freq=700.0))
        silence_id = job_silence.id
        await run_job(s, silence_id, provider=fake)

    fake.poll_result = "rate_limit"
    async with async_session_factory() as s:
        _, job_rl = await upload_bytes(s, a1, "rl.wav", make_wav(2.0, freq=720.0))
        rl_id = job_rl.id
        await run_job(s, rl_id, provider=fake)

    r_silence = await client.get(f"/api/jobs/{silence_id}")
    r_rl = await client.get(f"/api/jobs/{rl_id}")

    record(
        "P02-12",
        "GET /jobs/{id} 持久状态 + 静音/限流分型",
        r_silence.status_code == 200 and r_silence.json().get("error_type") == "no_speech"
        and r_rl.status_code == 200 and r_rl.json().get("error_type") == "rate_limited"
        and r_rl.json().get("status") == "retry_wait",
        dict(silence=r_silence.json().get("error_type"), rate_limit=r_rl.json().get("error_type"),
             rl_status=r_rl.json().get("status")),
    )
    clear_provider_override()


# ── P02-13 worker 失联恢复 + 有界重试 ───────────────────────────────
async def p02_13(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    a1, _ = await create_lecture(SUBJECT_A, "p0213-a")
    current["subject_id"] = SUBJECT_A
    wav = make_wav(4.0, freq=800.0)

    # 1. worker 失联：running + 心跳超 T → 恢复
    async with async_session_factory() as s:
        _, job = await upload_bytes(s, a1, "rec.wav", wav)
        job_id = job.id
    async with async_session_factory() as s:
        j = await s.get(ProcessingJob, job_id)
        j.status = "running"
        j.heartbeat_at = now() - timedelta(seconds=settings.JOB_HEARTBEAT_TIMEOUT_SECONDS + 5)
        j.attempt = 1
        await s.commit()

    fake = FakeASRProvider()
    set_provider(fake)
    async with async_session_factory() as s:
        stats = await recover_and_dispatch(s, provider=fake)
    async with async_session_factory() as s:
        j = await s.get(ProcessingJob, job_id)
        recovered = stats["recovered"] >= 1 and j.status == "succeeded"

    # 2. 连续限流到 R → 明确终态，不无限重试
    fake2 = FakeASRProvider()
    fake2.submit_result = "rate_limit"
    set_provider(fake2)
    async with async_session_factory() as s:
        _, job2 = await upload_bytes(s, a1, "rl2.wav", make_wav(4.0, freq=820.0))
        job2_id = job2.id
        for _ in range(settings.JOB_MAX_ATTEMPTS):
            await run_job(s, job2_id, provider=fake2)
        j2 = await s.get(ProcessingJob, job2_id)
        bounded = j2.status == "failed" and j2.attempt == settings.JOB_MAX_ATTEMPTS

    record(
        "P02-13",
        "worker 失联可恢复 + 重试有界（R 次后终态）",
        recovered and bounded,
        dict(recovered=recovered, recovered_stats=stats, bounded_status=j2.status, attempts=j2.attempt),
    )
    clear_provider_override()


# ── P02-14 显式重试/取消 ────────────────────────────────────────────
async def p02_14(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    fake = FakeASRProvider()
    fake.submit_result = "rate_limit"
    set_provider(fake)
    a1, _ = await create_lecture(SUBJECT_A, "p0214-a")
    current["subject_id"] = SUBJECT_A
    wav = make_wav(2.0, freq=900.0)

    async with async_session_factory() as s:
        _, job = await upload_bytes(s, a1, "retry.wav", wav)
        job_id = job.id
        for _ in range(settings.JOB_MAX_ATTEMPTS):
            await run_job(s, job_id, provider=fake)
        j = await s.get(ProcessingJob, job_id)
        failed_before = j.status == "failed"

    # 显式重试 → queued、attempt 归零
    r_retry = await client.post(f"/api/jobs/{job_id}/retry")
    async with async_session_factory() as s:
        j = await s.get(ProcessingJob, job_id)
        retried = j.status == "queued" and j.attempt == 0

    # 取消 + 重复取消稳定
    r_cancel1 = await client.post(f"/api/jobs/{job_id}/cancel")
    r_cancel2 = await client.post(f"/api/jobs/{job_id}/cancel")
    async with async_session_factory() as s:
        j = await s.get(ProcessingJob, job_id)
        cancelled = j.status == "cancelled" and j.cancel_requested is True

    record(
        "P02-14",
        "显式重试恢复 queued + 重复取消稳定",
        failed_before and retried and r_retry.status_code == 200
        and r_cancel1.status_code == 200 and r_cancel2.status_code == 200 and cancelled,
        dict(failed_before=failed_before, retry_status=r_retry.status_code,
             cancel1=r_cancel1.status_code, cancel2=r_cancel2.status_code, final=j.status),
    )
    clear_provider_override()


# ── P02-15 阶段一头含数据升级 + 数据/ID 保留 ────────────────────────
async def p02_15():
    # 路径 1：主测试库空库 → head
    conn = await asyncpg.connect(pg_dsn(MAIN_DB))
    try:
        main_head = await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()

    # 路径 2：阶段一头含数据 → head
    db2 = PREFIX + "p1data"
    DBS.append(db2)
    await create_db(db2)
    run_alembic("f7a8b9c0d1e2", asyncpg_dsn(db2))
    cid, mid, aid, vid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn = await asyncpg.connect(pg_dsn(db2))
    try:
        await conn.execute(
            "INSERT INTO courses (id, owner_id, title, created_at, updated_at) VALUES ($1,$2,$3,now(),now())",
            cid, SUBJECT_A, "p1-course",
        )
        await conn.execute(
            "INSERT INTO meetings (id, course_id, title, status, legacy_meeting, lecture_date) "
            "VALUES ($1,$2,$3,'pending',false,now())",
            mid, cid, "p1-lecture",
        )
        await conn.execute(
            "INSERT INTO audio_assets (id, lecture_id, storage_key, version, is_demo, created_at) "
            "VALUES ($1,$2,$3,1,false,now())",
            aid, mid, "assets/old.wav",
        )
        await conn.execute(
            "INSERT INTO transcript_versions (id, audio_asset_id, lecture_id, version, provider, status, created_at) "
            "VALUES ($1,$2,$3,1,'real','active',now())",
            vid, aid, mid,
        )
        await conn.execute(
            "INSERT INTO transcript_segments (id, version_id, seq_index, speaker, content, start_ms, end_ms, created_at) "
            "VALUES ($1,$2,0,'a','旧片段',0,1000,now())",
            sid, vid,
        )
        await conn.execute(
            "INSERT INTO evidence_spans (id, transcript_version_id, segment_start, segment_end, is_valid, created_at) "
            "VALUES ($1,$2,0,0,true,now())",
            uuid.uuid4(), vid,
        )
    finally:
        await conn.close()

    run_alembic("head", asyncpg_dsn(db2))
    conn = await asyncpg.connect(pg_dsn(db2))
    try:
        head = await conn.fetchval("SELECT version_num FROM alembic_version")
        kept = (
            await conn.fetchval("SELECT count(*) FROM courses WHERE id = $1", cid),
            await conn.fetchval("SELECT count(*) FROM meetings WHERE id = $1", mid),
            await conn.fetchval("SELECT count(*) FROM audio_assets WHERE id = $1", aid),
            await conn.fetchval("SELECT count(*) FROM transcript_versions WHERE id = $1", vid),
            await conn.fetchval("SELECT count(*) FROM transcript_segments WHERE id = $1", sid),
        )
        jobs_table = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='processing_jobs' AND table_schema='public'"
        )
        outbox_table = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='outbox_events' AND table_schema='public'"
        )
        all_kept = all(k == 1 for k in kept)
    finally:
        await conn.close()

    record(
        "P02-15",
        "空库与阶段一头含数据均升级到 head + 数据/ID 保留 + 新结构就位",
        main_head == "a2b3c4d5e6f7" and head == "a2b3c4d5e6f7"
        and all_kept and jobs_table == 1 and outbox_table == 1,
        dict(main_head=main_head, head=head, kept=kept,
             jobs_table=jobs_table, outbox_table=outbox_table),
    )


# ── P02-16 错误契约 + 不泄露凭证 ────────────────────────────────────
async def p02_16(current, client):
    clear_provider_override()
    settings.ASR_MODE = "real"
    a1, _ = await create_lecture(SUBJECT_A, "p0216-a")
    current["subject_id"] = SUBJECT_A

    r_bad = await client.post(f"/api/lectures/{a1}/audio", files={"file": ("x.wav", b"not audio", "audio/wav")})
    bad_shape = {"code", "message", "retryable", "request_id"} <= set(r_bad.json())
    no_key_in_resp = "sk-" not in json.dumps(r_bad.json())

    r_404 = await client.get(f"/api/jobs/{uuid.uuid4()}")
    notfound_shape = {"code", "message", "retryable", "request_id"} <= set(r_404.json())

    record(
        "P02-16",
        "错误响应含 code/message/retryable/request_id 且不泄露凭证",
        r_bad.status_code == 422 and bad_shape and r_404.status_code == 404
        and notfound_shape and no_key_in_resp,
        dict(bad_shape=bad_shape, notfound_shape=notfound_shape, bad_body=r_bad.json()),
    )


if __name__ == "__main__":
    asyncio.run(main())
