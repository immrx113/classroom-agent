"""课时 API 路由"""

import uuid

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.models.meeting import Meeting
from app.schemas.job import JobResponse, UploadAudioResponse
from app.schemas.lecture import LectureResponse, TranscriptSegmentResponse
from app.services.access import enforce_writable, get_accessible_lecture
from app.services.audio_asset_service import audio_asset_service
from app.services.index_service import index_service
from app.services.job_service import asr_job_to_response, get_latest_asr_job
from app.services.lecture_service import lecture_service
from app.services.rate_limit_service import FAMILY_AUDIO_UPLOAD, enforce_user_bucket
from app.services.scope import ScopeContext

router = APIRouter(prefix="/lectures", tags=["课时"])


@router.get("/{lecture_id}", response_model=LectureResponse)
async def get_lecture(
    lecture_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> LectureResponse:
    """返回阶段状态、当前有效版本和资产摘要"""
    lecture, summary = await lecture_service.get_lecture(db, scope, lecture_id)
    return LectureResponse(
        id=lecture.id,
        course_id=lecture.course_id,
        title=lecture.title,
        description=lecture.description,
        lecture_date=lecture.lecture_date,
        status=lecture.status,
        legacy_meeting=lecture.legacy_meeting,
        active_audio=summary["active_audio"],
        active_transcript=summary["active_transcript"],
    )


@router.get("/{lecture_id}/transcript", response_model=list[TranscriptSegmentResponse])
async def list_transcript(
    lecture_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> list[TranscriptSegmentResponse]:
    """列出课时当前有效转写片段（按顺序，含毫秒定位，供点选回听）。"""
    segments = await lecture_service.list_segments(db, scope, lecture_id)
    return [TranscriptSegmentResponse.model_validate(s) for s in segments]


@router.get("/{lecture_id}/job", response_model=JobResponse | None)
async def get_latest_job(
    lecture_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """课时最新的 ASR 任务（失败/重试等待等），供页面刷新后恢复失败原因与重试按钮。"""
    await get_accessible_lecture(db, scope, lecture_id)
    job = await get_latest_asr_job(db, lecture_id)
    if job is None:
        return None
    return asr_job_to_response(job)


@router.get("/{lecture_id}/index-status")
async def get_index_status(
    lecture_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """课时所属课程的检索索引状态（active/构建中/无）。"""
    return await index_service.get_index_status(db, scope, lecture_id)


@router.post("/{lecture_id}/audio", response_model=UploadAudioResponse, status_code=201)
async def upload_audio(
    lecture_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
    new_version: bool = Query(False, description="显式发起新版本，跳过同内容去重"),
) -> UploadAudioResponse:
    """完整文件流式上传，校验后返回 asset_id 与处理 job_id。

    归属/归档/范围统一经 get_accessible_lecture 校验；媒体校验与落库见
    audio_asset_service.upload。同一课时同一内容的重复上传（业务请求重试）
    返回既有 asset_id/job_id，不重复创建任务；new_version=true 时明确创建新版本。
    """
    lecture = await get_accessible_lecture(db, scope, lecture_id)
    # 回退只读（CA-25）：停写新任务入口；越权先 404、无副作用。
    enforce_writable()
    # 用户入口令牌桶（CA-14）：限流拒绝时无 asset/job/outbox。
    await enforce_user_bucket(scope, FAMILY_AUDIO_UPLOAD)
    asset, job = await audio_asset_service.upload(
        db, lecture_id=lecture.id, filename=file.filename or "audio", file=file,
        subject_id=scope.subject_id, new_version=new_version,
    )
    return UploadAudioResponse(asset_id=asset.id, job_id=job.id)
