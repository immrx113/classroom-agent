"""课程与课时业务逻辑层

统一处理归属与范围规则：所有读写均以 ScopeContext 中的 subject_id 为边界，
course_id 只表示请求范围，不能替代权限检查。可访问条件统一来自
``app.services.access``（归属 + 未归档 + 范围约束）。
"""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.course import Course
from app.models.meeting import Meeting
from app.schemas.course import CourseCreate, LectureCreate
from app.services.access import get_accessible_course, get_owned_course, course_accessible_cond
from app.services.scope import ScopeContext


class CourseService:
    async def _get_owned_course(
        self, db: AsyncSession, scope: ScopeContext, course_id: uuid.UUID
    ) -> Course:
        """按归属主体 + 未归档 + 范围约束获取课程，权限不明时拒绝（404 不泄露存在性）"""
        return await get_accessible_course(db, scope, course_id)

    async def create_course(
        self, db: AsyncSession, scope: ScopeContext, data: CourseCreate
    ) -> Course:
        course = Course(
            owner_id=scope.subject_id,
            title=data.title,
            teacher=data.teacher,
            term=data.term,
        )
        db.add(course)
        await db.flush()
        await db.refresh(course)
        return course

    async def list_courses(
        self, db: AsyncSession, scope: ScopeContext, page: int, page_size: int
    ) -> tuple[list[Course], int, dict[uuid.UUID, int]]:
        """分页返回可访问课程 + 课时统计（默认不含已归档）"""
        cond = course_accessible_cond(scope)

        total = (await db.execute(select(func.count(Course.id)).where(cond))).scalar_one()
        rows = (
            await db.execute(
                select(Course)
                .where(cond)
                .order_by(Course.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()

        counts: dict[uuid.UUID, int] = {}
        course_ids = [c.id for c in rows]
        if course_ids:
            count_cond = Meeting.course_id.in_(course_ids)
            if scope.lecture_id is not None:
                # 显式课时范围：统计只计允许课时，不含同课其他课时（M01-04）
                count_cond = and_(count_cond, Meeting.id == scope.lecture_id)
            cnt_rows = (
                await db.execute(
                    select(Meeting.course_id, func.count(Meeting.id))
                    .where(count_cond)
                    .group_by(Meeting.course_id)
                )
            ).all()
            counts = {cid: n for cid, n in cnt_rows}

        return list(rows), total, counts

    async def delete_course(
        self, db: AsyncSession, scope: ScopeContext, course_id: uuid.UUID
    ) -> None:
        """标记删除并立即停止检索暴露；物理清理由后续任务异步执行。

        幂等：已归档课程再次删除不报错（仍校验归属，越权仍 404）。
        """
        course = await get_owned_course(db, scope, course_id)
        if course.archived_at is None:
            course.archived_at = datetime.now(timezone.utc)
            await db.flush()

    async def ensure_history_course(
        self, db: AsyncSession, scope: ScopeContext
    ) -> Course:
        """取得（或创建）「待整理历史资料」课程，用于旧入口创建的会议挂载"""
        row = (
            await db.execute(
                select(Course)
                .where(Course.owner_id == scope.subject_id, Course.term == "历史导入")
                .order_by(Course.created_at.asc())
                .limit(1)
            )
        ).scalars().first()
        if row is not None:
            return row
        course = Course(
            owner_id=scope.subject_id,
            title="待整理历史资料",
            term="历史导入",
        )
        db.add(course)
        await db.flush()
        await db.refresh(course)
        return course

    async def create_lecture(
        self,
        db: AsyncSession,
        scope: ScopeContext,
        course_id: uuid.UUID,
        data: LectureCreate,
    ) -> Meeting:
        """在当前用户课程下创建课时（已归档课程不可新建课时）"""
        await self._get_owned_course(db, scope, course_id)
        lecture = Meeting(
            course_id=course_id,
            title=data.title,
            description=data.description,
            lecture_date=data.lecture_date,
            status="pending",
        )
        db.add(lecture)
        await db.flush()
        await db.refresh(lecture)
        return lecture


# 全局实例
course_service = CourseService()
