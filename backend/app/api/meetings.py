"""会议 API 路由

兼容期：meetings 物理表即课时存储。旧会议入口统一复用访问控制
（归属 + 未归档 + 范围约束），旧列表不泄露其他主体的课时；
新建会议归入「待整理历史资料」课程，不产生无归属课时。
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_meeting_or_404, get_scope
from app.models.meeting import Meeting
from app.schemas.meeting import (
    MeetingCreate,
    MeetingUpdate,
    MeetingResponse,
    TranscriptResponse,
    TranscriptionStatusResponse,
)
from app.services.access import accessible_lecture_ids
from app.services.course_service import course_service
from app.services.meeting_service import meeting_service
from app.services.scope import ScopeContext

router = APIRouter(prefix="/meetings", tags=["会议管理"])


@router.post("", response_model=MeetingResponse, status_code=201)
async def create_meeting(
    data: MeetingCreate,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> Meeting:
    """创建会议（兼容期：归入历史资料课程，标记 legacy）"""
    course = await course_service.ensure_history_course(db, scope)
    meeting = Meeting(
        course_id=course.id,
        title=data.title,
        description=data.description,
        participants=data.participants,
        start_time=data.start_time,
        end_time=data.end_time,
        status="pending",
        legacy_meeting=True,
    )
    db.add(meeting)
    await db.flush()
    await db.refresh(meeting)
    return meeting


@router.get("", response_model=list[MeetingResponse])
async def list_meetings(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> list[Meeting]:
    """获取会议列表（仅当前主体可访问的课时）"""
    skip = (page - 1) * page_size
    result = await db.execute(
        select(Meeting)
        .where(Meeting.id.in_(accessible_lecture_ids(scope)))
        .order_by(Meeting.created_at.desc())
        .offset(skip)
        .limit(page_size)
    )
    return list(result.scalars().all())


@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(meeting: Meeting = Depends(get_meeting_or_404)) -> Meeting:
    """获取会议详情"""
    return meeting


@router.patch("/{meeting_id}", response_model=MeetingResponse)
async def update_meeting(
    data: MeetingUpdate,
    db: AsyncSession = Depends(get_db),
    meeting: Meeting = Depends(get_meeting_or_404),
) -> Meeting:
    """更新会议"""
    updated = await meeting_service.update_meeting(db, meeting.id, data)
    return updated  # type: ignore[return-value]


@router.delete("/{meeting_id}", status_code=204)
async def delete_meeting(
    db: AsyncSession = Depends(get_db),
    meeting: Meeting = Depends(get_meeting_or_404),
) -> None:
    """删除会议"""
    await meeting_service.delete_meeting(db, meeting.id)


@router.post("/{meeting_id}/upload", status_code=410)
async def upload_audio(
    meeting: Meeting = Depends(get_meeting_or_404),
) -> dict:
    """旧上传写入口已明确停用（P02-01）。

    第二阶段统一走新上传接口 ``POST /lectures/{id}/audio``，旧入口不再执行
    任何写入，避免绕过媒体校验 / 事务 outbox / 真实失败不降级等新约束。
    """
    raise HTTPException(
        status_code=410,
        detail="旧上传入口已停用，请使用 POST /lectures/{id}/audio",
    )


@router.get("/{meeting_id}/audio")
async def get_audio(
    meeting: Meeting = Depends(get_meeting_or_404),
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """获取音频文件"""
    if meeting.active_audio_id is not None:
        from app.services.audio_asset_service import audio_asset_service
        path, mime, _ = await audio_asset_service.resolve_asset(
            db, ScopeContext(subject_id=scope.subject_id, course_id=meeting.course_id,
                             lecture_id=meeting.id), meeting.active_audio_id)
        return FileResponse(path, media_type=mime, filename=os.path.basename(path))
    if not meeting.audio_url or not os.path.exists(meeting.audio_url):
        raise HTTPException(status_code=404, detail="音频文件不存在")
    return FileResponse(
        meeting.audio_url,
        media_type="audio/mpeg",
        filename=os.path.basename(meeting.audio_url),
    )


@router.get("/{meeting_id}/transcripts", response_model=list[TranscriptResponse])
async def get_transcripts(
    db: AsyncSession = Depends(get_db),
    meeting: Meeting = Depends(get_meeting_or_404),
) -> list:
    """获取会议转写记录"""
    return await meeting_service.get_transcripts(db, meeting.id)


@router.get("/{meeting_id}/transcription-status", response_model=TranscriptionStatusResponse)
async def get_transcription_status(
    meeting: Meeting = Depends(get_meeting_or_404),
    db: AsyncSession = Depends(get_db),
) -> TranscriptionStatusResponse:
    """获取转写状态"""
    count = await meeting_service.get_transcript_count(db, meeting.id)
    return TranscriptionStatusResponse(
        meeting_id=meeting.id,
        status=meeting.status,
        transcript_count=count,
    )
