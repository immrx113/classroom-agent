"""统一访问控制层

所有以课程 / 课时 / 证据 / 会话 / 知识为资源的读取、写入、来源解析统一经过这里，
保证以下规则一致生效（对应验收 R2 / R6，复验 F1 / F5）：

- 归属：``course.owner_id == scope.subject_id``；会话 / 知识直接以 owner_id 归属，
  不再存在「NULL 关联即所有人可见」的默认规则
- 归档即不可见：``course.archived_at IS NULL``（经课时/来源间接关联的资源同样被隐藏）
- ``ScopeContext`` 中非空的 ``course_id`` / ``lecture_id`` 落实为约束，
  不匹配即拒绝，不自动放宽为主体的全量范围
- 权限不明一律 404（不泄露存在性），不回退成全库查询

新增的旧入口（meetings / summaries / chat / decisions / knowledge）与新的
course / lecture / evidence 服务均复用这里的查询条件，避免散落实现。
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.chat import ChatSession
from app.models.course import Course
from app.models.index_version import IndexBlock, IndexVersion
from app.models.knowledge_doc import KnowledgeDocument
from app.models.meeting import Meeting
from app.models.source_document import SourceDocument, SourceDocumentVersion
from app.services.scope import ScopeContext


def course_accessible_cond(scope: ScopeContext) -> ColumnElement[bool]:
    """可访问课程条件：归属 + 未归档 + 可选课程/课时范围约束。

    指定 ``lecture_id`` 时，课程必须包含该课时（课时真实属于范围内课程），
    冲突或越权范围自然得不到任何行。
    """
    cond = and_(
        Course.owner_id == scope.subject_id,
        Course.archived_at.is_(None),
    )
    if scope.course_id is not None:
        cond = and_(cond, Course.id == scope.course_id)
    if scope.lecture_id is not None:
        lecture_course = select(Meeting.course_id).where(
            Meeting.id == scope.lecture_id
        )
        cond = and_(cond, Course.id.in_(lecture_course))
    return cond


async def get_accessible_course(
    db: AsyncSession, scope: ScopeContext, course_id: uuid.UUID
) -> Course:
    """返回可访问课程；越权 / 已归档 / 范围不匹配均 404"""
    course = (
        await db.execute(
            select(Course).where(Course.id == course_id, course_accessible_cond(scope))
        )
    ).scalar_one_or_none()
    if course is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    return course


async def get_owned_course(
    db: AsyncSession, scope: ScopeContext, course_id: uuid.UUID
) -> Course:
    """按归属主体获取课程，不校验归档（供删除等幂等操作使用）；越权 / 范围不匹配 404"""
    cond = and_(
        Course.id == course_id,
        Course.owner_id == scope.subject_id,
    )
    if scope.course_id is not None:
        cond = and_(cond, Course.id == scope.course_id)
    if scope.lecture_id is not None:
        lecture_course = select(Meeting.course_id).where(
            Meeting.id == scope.lecture_id
        )
        cond = and_(cond, Course.id.in_(lecture_course))
    course = (await db.execute(select(Course).where(cond))).scalar_one_or_none()
    if course is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    return course


async def get_accessible_lecture(
    db: AsyncSession, scope: ScopeContext, lecture_id: uuid.UUID
) -> Meeting:
    """返回可访问课时；归属 / 归档 / 范围约束 / 课程-课时关系均校验"""
    cond = and_(
        Meeting.id == lecture_id,
        Meeting.course_id == Course.id,
        course_accessible_cond(scope),
    )
    if scope.lecture_id is not None:
        cond = and_(cond, Meeting.id == scope.lecture_id)
    lecture = (
        await db.execute(
            select(Meeting)
            .join(Course, Meeting.course_id == Course.id)
            .where(cond)
        )
    ).scalar_one_or_none()
    if lecture is None:
        raise HTTPException(status_code=404, detail="课时不存在")
    return lecture


async def get_accessible_document(
    db: AsyncSession, scope: ScopeContext, document_id: uuid.UUID
) -> SourceDocument:
    """返回可访问文档；越权 / 归档 / 范围不匹配均 404。"""
    doc = (
        await db.execute(
            select(SourceDocument).where(
                SourceDocument.id == document_id, document_accessible_cond(scope)
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


async def get_accessible_document_version(
    db: AsyncSession, scope: ScopeContext, version_id: uuid.UUID
) -> SourceDocumentVersion:
    """返回可访问文档版本；越权 / 归档 / 范围不匹配均 404。"""
    ver = (
        await db.execute(
            select(SourceDocumentVersion).where(
                SourceDocumentVersion.id == version_id,
                document_version_accessible_cond(scope),
            )
        )
    ).scalar_one_or_none()
    if ver is None:
        raise HTTPException(status_code=404, detail="文档版本不存在")
    return ver


def is_read_only() -> bool:
    """回退只读开关（CA-25）：True 时停止新任务入口与 worker 领取/外发/发布。

    供 worker / 派发器在 HTTP 路由之外复用同一开关，避免散落重复判断。
    """
    from app.config import settings

    return bool(settings.CLASSROOM_READ_ONLY)


def enforce_writable() -> None:
    """课堂新任务入口停写（回退只读，CA-25）。

    置 ``settings.CLASSROOM_READ_ONLY`` 为真时，音频上传 / 整理 / 显式索引 /
    课件文档 / 手动重试等新任务入口一律 503，只读原有 active 结果继续可用；
    不降级为旧无控制付费路径、不删除新增表/调用记录/账本。
    """
    from app.services.errors import ServiceError

    if is_read_only():
        raise ServiceError(
            503, "read_only", "课堂写入已停用（回退只读），仅可读取已有结果",
            retryable=False,
        )


def accessible_lecture_ids(scope: ScopeContext):
    """当前主体可访问的课时 ID 子查询（供旧列表入口过滤）"""
    cond = and_(
        Meeting.course_id == Course.id,
        Course.owner_id == scope.subject_id,
        Course.archived_at.is_(None),
    )
    if scope.course_id is not None:
        cond = and_(cond, Meeting.course_id == scope.course_id)
    if scope.lecture_id is not None:
        cond = and_(cond, Meeting.id == scope.lecture_id)
    return (
        select(Meeting.id)
        .join(Course, Meeting.course_id == Course.id)
        .where(cond)
    )


def chat_session_accessible_cond(scope: ScopeContext) -> ColumnElement[bool]:
    """可访问会话条件：归属主体 + （全局会话 或 关联课时可访问）。"""
    return and_(
        ChatSession.owner_id == scope.subject_id,
        or_(
            ChatSession.meeting_id.is_(None),
            ChatSession.meeting_id.in_(accessible_lecture_ids(scope)),
        ),
    )


def knowledge_accessible_cond(scope: ScopeContext) -> ColumnElement[bool]:
    """可访问知识条件：归属主体 + 排除指向已归档课程会议的来源。

    说明：知识块按 owner_id 隔离；uploaded_doc 无来源会议时仅按 owner 隔离。
    课程/课时范围收窄待模块 05 在知识模型上显式课程化后再接入。
    """
    archived_meetings = (
        select(Meeting.id)
        .join(Course, Meeting.course_id == Course.id)
        .where(Course.archived_at.is_not(None))
    )
    return and_(
        KnowledgeDocument.owner_id == scope.subject_id,
        or_(
            KnowledgeDocument.source_id.is_(None),
            KnowledgeDocument.source_id.not_in(archived_meetings),
        ),
    )


def _accessible_course_ids(scope: ScopeContext):
    """当前主体可访问（归属 + 未归档）的课程 ID 子查询。"""
    return select(Course.id).where(
        and_(Course.owner_id == scope.subject_id, Course.archived_at.is_(None))
    )


def document_accessible_cond(scope: ScopeContext) -> ColumnElement[bool]:
    """可访问文档条件：文档归属课程必须属于主体且未归档；可选课程/课时范围约束。

    课时范围：文档为课程公共课件（lecture_id 空）或属于指定课时本身，均可见。
    """
    cond = SourceDocument.course_id.in_(_accessible_course_ids(scope))
    if scope.course_id is not None:
        cond = and_(cond, SourceDocument.course_id == scope.course_id)
    if scope.lecture_id is not None:
        cond = and_(
            cond,
            or_(
                SourceDocument.lecture_id.is_(None),
                SourceDocument.lecture_id == scope.lecture_id,
            ),
        )
    return cond


def document_version_accessible_cond(scope: ScopeContext) -> ColumnElement[bool]:
    """可访问文档版本条件（经文档归属课程收敛）。"""
    return SourceDocumentVersion.source_document_id.in_(
        select(SourceDocument.id).where(document_accessible_cond(scope))
    )


def index_version_accessible_cond(scope: ScopeContext) -> ColumnElement[bool]:
    """可访问索引版本条件：版本所属课程属于主体且未归档；可选课程范围约束。"""
    cond = IndexVersion.course_id.in_(_accessible_course_ids(scope))
    if scope.course_id is not None:
        cond = and_(cond, IndexVersion.course_id == scope.course_id)
    if scope.lecture_id is not None:
        lecture_course = select(Meeting.course_id).where(Meeting.id == scope.lecture_id)
        cond = and_(cond, IndexVersion.course_id.in_(lecture_course))
    return cond


def index_block_accessible_cond(scope: ScopeContext) -> ColumnElement[bool]:
    """可访问索引块条件：块所属课程属于主体且未归档；可选课程/课时范围约束。

    课时范围：指定 L1 可检索 L1 专属来源及该课程公共课件（lecture_id 空），
    不能检索同课程其他课时专属来源。
    """
    cond = IndexBlock.course_id.in_(_accessible_course_ids(scope))
    if scope.course_id is not None:
        cond = and_(cond, IndexBlock.course_id == scope.course_id)
    if scope.lecture_id is not None:
        cond = and_(
            cond,
            or_(
                IndexBlock.lecture_id.is_(None),
                IndexBlock.lecture_id == scope.lecture_id,
            ),
        )
    return cond
