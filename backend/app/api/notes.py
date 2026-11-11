"""课堂笔记接口（第三阶段「全课整理」）"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.schemas.job import JobResponse
from app.schemas.note import EditNoteRequest, NoteResponse, OrganizeRequest
from app.services.access import enforce_writable, get_accessible_lecture
from app.services.organize_service import organize_service
from app.services.rate_limit_service import FAMILY_ORGANIZE, enforce_user_bucket
from app.services.scope import ScopeContext

router = APIRouter(tags=["笔记"])


def _note_response(note) -> NoteResponse:
    return NoteResponse(
        id=note.id,
        lecture_id=note.lecture_id,
        transcript_version_id=note.transcript_version_id,
        pipeline_version=note.pipeline_version,
        status=note.status,
        content=note.content,
        previous_version_id=note.previous_version_id,
        edited_at=note.edited_at,
        published_at=note.published_at,
        created_at=note.created_at,
    )


@router.post("/lectures/{lecture_id}/organize", response_model=JobResponse, status_code=201)
async def create_organize_job(
    lecture_id: uuid.UUID,
    body: OrganizeRequest,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> JobResponse:
    """对指定转写版本发起全课整理，返回持久任务。"""
    # 越权先 404、无副作用；回退只读停写（CA-25）；用户入口令牌桶拒绝时无新 job/outbox（CA-14）。
    await get_accessible_lecture(db, scope, lecture_id)
    enforce_writable()
    await enforce_user_bucket(scope, FAMILY_ORGANIZE)
    job = await organize_service.create_organize_job(
        db, scope, lecture_id=lecture_id, transcript_version_id=body.transcript_version_id
    )
    return JobResponse(
        id=job.id,
        type=job.type,
        resource_id=job.resource_id,
        input_version=job.input_version,
        status=job.status,
        stage=job.stage,
        attempt=job.attempt or 0,
        cancel_requested=job.cancel_requested,
        error_code=job.error_code,
        error_type=job.error_type,
        retryable=False,
        provider_task_id=job.provider_task_id,
        output_version=job.output_version,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get("/lectures/{lecture_id}/notes", response_model=list[NoteResponse])
async def list_notes(
    lecture_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> list[NoteResponse]:
    notes = await organize_service.get_notes(db, scope, lecture_id)
    return [_note_response(n) for n in notes]


@router.get("/lectures/{lecture_id}/notes/current", response_model=Optional[NoteResponse])
async def get_current_note(
    lecture_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> Optional[NoteResponse]:
    note = await organize_service.get_current_note(db, scope, lecture_id)
    return _note_response(note) if note is not None else None


@router.get("/notes/{note_id}", response_model=NoteResponse)
async def get_note(
    note_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> NoteResponse:
    note = await organize_service.get_note(db, scope, note_id)
    return _note_response(note)


@router.patch("/notes/{note_id}", response_model=NoteResponse)
async def edit_note(
    note_id: uuid.UUID,
    body: EditNoteRequest,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> NoteResponse:
    patch = body.model_dump(exclude_unset=True)
    note = await organize_service.edit_note(db, scope, note_id, patch)
    return _note_response(note)


@router.post("/notes/{note_id}/publish", response_model=NoteResponse)
async def publish_note(
    note_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> NoteResponse:
    note = await organize_service.publish_note(db, scope, note_id)
    return _note_response(note)
