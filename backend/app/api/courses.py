"""课程 API 路由"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.models.course import Course
from app.models.meeting import Meeting
from app.schemas.course import (
    CourseCreate,
    CourseResponse,
    CourseListItem,
    CourseListResponse,
    LectureCreate,
)
from app.schemas.lecture import LectureListItem, LectureResponse
from app.services.access import enforce_writable, get_accessible_course
from app.services.course_service import course_service
from app.services.index_service import index_service
from app.services.rate_limit_service import FAMILY_EXPLICIT_INDEX, enforce_user_bucket
from app.services.scope import ScopeContext

router = APIRouter(prefix="/courses", tags=["课程"])


@router.post("", response_model=CourseResponse, status_code=201)
async def create_course(
    data: CourseCreate,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> Course:
    """创建当前用户所属课程"""
    return await course_service.create_course(db, scope, data)


@router.get("", response_model=CourseListResponse)
async def list_courses(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> CourseListResponse:
    """分页返回可访问课程与课时统计"""
    rows, total, counts = await course_service.list_courses(db, scope, page, page_size)
    items = [
        CourseListItem(
            **CourseResponse.model_validate(row).model_dump(),
            lecture_count=counts.get(row.id, 0),
        )
        for row in rows
    ]
    return CourseListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/{course_id}/lectures", response_model=list[LectureListItem])
async def list_lectures(
    course_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> list[LectureListItem]:
    """列出课程下课时（供范围选择 / 索引状态查看）。"""
    await get_accessible_course(db, scope, course_id)
    rows = (
        await db.execute(
            select(Meeting)
            .where(Meeting.course_id == course_id)
            .order_by(Meeting.created_at.asc())
        )
    ).scalars().all()
    return [LectureListItem.model_validate(r) for r in rows]


@router.post("/{course_id}/lectures", response_model=LectureResponse, status_code=201)
async def create_lecture(
    course_id: uuid.UUID,
    data: LectureCreate,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> LectureResponse:
    """在课程下创建课时"""
    lecture = await course_service.create_lecture(db, scope, course_id, data)
    return LectureResponse(
        id=lecture.id,
        course_id=lecture.course_id,
        title=lecture.title,
        description=lecture.description,
        lecture_date=lecture.lecture_date,
        status=lecture.status,
        legacy_meeting=lecture.legacy_meeting,
    )


@router.delete("/{course_id}", status_code=204)
async def delete_course(
    course_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> None:
    """标记删除课程并立即停止检索暴露"""
    await course_service.delete_course(db, scope, course_id)


@router.post("/{course_id}/index", status_code=201)
async def create_index(
    course_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """显式构建课程检索索引（CA-14 入口保护：显式索引构建）。

    越权先 404、无副作用；用户入口令牌桶拒绝时无新 job/outbox。
    """
    await get_accessible_course(db, scope, course_id)
    enforce_writable()
    await enforce_user_bucket(scope, FAMILY_EXPLICIT_INDEX)
    job = await index_service.create_index_job(db, scope, course_id=course_id)
    return {"job_id": job.id, "course_id": course_id, "status": job.status}
