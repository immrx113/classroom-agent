"""范围对象与身份

服务端确定当前用户；course_id / lecture_id 只是请求范围，不替代权限检查。
个人版固定本地主体，与未来登录身份共用同一 ScopeContext 检查路径。
SQL/向量/全文/工具/证据读取均使用同一范围对象。
"""

import uuid
from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class ScopeContext:
    """请求范围：身份 + 可选课程/课时约束"""

    subject_id: str
    course_id: uuid.UUID | None = None
    lecture_id: uuid.UUID | None = None


def local_scope() -> ScopeContext:
    """个人版：固定本地主体。未来替换为登录身份解析，检查路径不变。"""
    return ScopeContext(subject_id=settings.LOCAL_SUBJECT_ID)
