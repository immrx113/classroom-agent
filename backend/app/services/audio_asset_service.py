"""音频资产上传 / 受控读取服务

上传按块读取、累计限大小；写入临时位置，校验通过后以服务端生成的
存储键确认为完整资产；完整资产、任务、outbox 在同一事务提交（P02-05/06/07）。
存储键由服务端生成（asset id + 探测出的容器扩展名），与客户端文件名无关。
"""

import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audio_asset import AudioAsset
from app.models.processing_job import ProcessingJob, OutboxEvent
from app.services import media_service
from app.services.access import get_accessible_lecture
from app.services.asr_provider import get_provider
from app.services.errors import ServiceError

logger = logging.getLogger(__name__)

# 测试钩子：在 asset+job+outbox flush 后、commit 前调用；测试可注入回滚。
_after_flush_hook: Optional[Callable[[], None]] = None

_EXT = {"wav": ".wav", "mp3": ".mp3", "flac": ".flac", "m4a": ".m4a"}


def _validate_filename(filename: str) -> None:
    """危险路径名拒绝：含路径分隔、父目录跳转或绝对路径/盘符的文件名不用于存储。"""
    if not filename:
        raise ServiceError(422, "invalid_filename", "缺少文件名", retryable=False)
    if "/" in filename or "\\" in filename:
        raise ServiceError(422, "invalid_filename", "文件名不得包含路径分隔符", retryable=False)
    parts = filename.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        raise ServiceError(422, "invalid_filename", "文件名不得包含父目录跳转", retryable=False)
    if filename.startswith("/") or filename.startswith("\\"):
        raise ServiceError(422, "invalid_filename", "文件名不得为绝对路径", retryable=False)
    if len(filename) >= 2 and filename[1] == ":":
        raise ServiceError(422, "invalid_filename", "文件名不得为绝对路径", retryable=False)


def _config_hash() -> str:
    payload = (
        f"{settings.MEDIA_UPLOAD_MAX_BYTES}|{settings.MEDIA_UPLOAD_CHUNK_BYTES}|"
        f"{settings.ASR_MODE}|{settings.TRANSCRIPTION_MODEL}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class AudioAssetService:
    async def upload(
        self,
        db: AsyncSession,
        *,
        lecture_id: uuid.UUID,
        filename: str,
        file,
        subject_id: str | None = None,
        new_version: bool = False,
    ) -> tuple[AudioAsset, ProcessingJob]:
        """完整文件流式上传，校验并落库资产 + 任务 + outbox。"""
        _validate_filename(filename)
        root = Path(settings.UPLOAD_DIR)
        tmp_dir = root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"{uuid.uuid4().hex}.part"

        final_path: Optional[Path] = None
        try:
            # 1. 按块读取 + 累计大小校验（不整文件 read-all）
            written = 0
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = await file.read(settings.MEDIA_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > settings.MEDIA_UPLOAD_MAX_BYTES:
                        raise ServiceError(
                            413, "upload_too_large",
                            f"上传超过大小上限 {settings.MEDIA_UPLOAD_MAX_BYTES} 字节",
                            retryable=False,
                        )
                    f.write(chunk)

            # 2. 真实媒体校验（容器/编码/可解码性/时长）
            info = media_service.probe(str(tmp_path))

            # 3. 摘要 + 服务端存储键 + 落盘完整资产
            checksum = _sha256(str(tmp_path))

            # 幂等：同一课时同一内容（checksum）的重复上传视为同一业务请求重试，
            # 返回既有资产与任务，不重复创建资产/任务/转写，也不重复提交供应商处理。
            # 显式 new_version 跳过去重，允许明确发起新版本。
            if not new_version:
                existing_asset = (
                    await db.execute(
                        select(AudioAsset)
                        .where(
                            AudioAsset.lecture_id == lecture_id,
                            AudioAsset.checksum == checksum,
                        )
                        .order_by(AudioAsset.created_at.asc())
                        .limit(1)
                    )
                ).scalars().first()
                if existing_asset is not None:
                    existing_job = (
                        await db.execute(
                            select(ProcessingJob)
                            .where(ProcessingJob.input_version == existing_asset.id)
                            .order_by(ProcessingJob.created_at.asc())
                            .limit(1)
                        )
                    ).scalars().first()
                    if existing_job is not None:
                        tmp_path.unlink(missing_ok=True)
                        logger.info(
                            f"重复上传命中既有资产 asset={existing_asset.id} "
                            f"job={existing_job.id} checksum={checksum}"
                        )
                        return existing_asset, existing_job

            asset_id = uuid.uuid4()
            storage_key = f"assets/{asset_id}{_EXT[info.container]}"
            final_path = root / storage_key
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, final_path)

            # 4. 同一事务：资产 + 任务 + outbox
            is_demo = get_provider().name == "demo"
            asset = AudioAsset(
                id=asset_id,
                lecture_id=lecture_id,
                storage_key=storage_key,
                checksum=checksum,
                mime=info.mime,
                duration_ms=info.duration_ms,
                size=info.size,
                version=1,
                is_demo=is_demo,
            )
            db.add(asset)
            await db.flush()

            job = ProcessingJob(
                type="asr",
                resource_id=lecture_id,
                input_version=asset.id,
                config_hash=_config_hash(),
                status="queued",
                stage="created",
                attempt=0,
                cancel_requested=False,
                # 可信主体归属：由服务端 scope 确定，客户端不可自选（CA-24）
                subject_id=subject_id or settings.LOCAL_SUBJECT_ID,
            )
            db.add(job)
            await db.flush()

            db.add(
                OutboxEvent(
                    business_key=f"job:{job.id}",
                    event_type="asr_requested",
                    job_id=job.id,
                    payload={"job_id": str(job.id), "resource_id": str(lecture_id)},
                    status="pending",
                    attempt=0,
                )
            )
            await db.flush()

            if _after_flush_hook is not None:
                _after_flush_hook()

            await db.commit()
            logger.info(
                f"上传完成 asset={asset.id} job={job.id} size={info.size} "
                f"container={info.container} duration_ms={info.duration_ms}"
            )
            return asset, job
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            if final_path is not None and final_path.exists():
                final_path.unlink(missing_ok=True)
            raise

    async def resolve_asset(
        self, db: AsyncSession, scope, asset_id: uuid.UUID
    ) -> tuple[str, str, int]:
        """受控读取：校验资产属于可访问课时，返回 (绝对路径, mime, size)。"""
        asset = await db.get(AudioAsset, asset_id)
        if asset is None or not asset.storage_key:
            raise ServiceError(404, "not_found", "音频资产不存在", retryable=False)
        # 授权检查：资产所属课时必须可访问（归属 + 未归档 + 范围约束）
        await get_accessible_lecture(db, scope, asset.lecture_id)

        path = Path(settings.UPLOAD_DIR) / asset.storage_key
        # 存储键为服务端生成，仍确保解析结果位于资产目录内，杜绝越目录读取
        root = Path(settings.UPLOAD_DIR).resolve()
        if not str(path.resolve()).startswith(str(root)):
            raise ServiceError(404, "not_found", "音频资产不存在", retryable=False)
        if not path.exists():
            raise ServiceError(404, "not_found", "音频资产不存在", retryable=False)
        return str(path), asset.mime or "application/octet-stream", asset.size or 0


audio_asset_service = AudioAssetService()
