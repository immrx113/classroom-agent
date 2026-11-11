"""API 依赖注入"""

import uuid
from typing import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.meeting import Meeting
from app.services.access import get_accessible_lecture
from app.services.scope import ScopeContext, local_scope


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库 Session"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_scope() -> ScopeContext:
    """当前用户范围：个人版固定主体；未来替换为登录身份解析，检查路径不变"""
    return local_scope()


async def get_meeting_or_404(
    meeting_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> Meeting:
    """获取会议（兼容期即课时），不存在 / 越权 / 已归档 / 范围不匹配均 404。

    meetings 物理表现在是课时存储，旧会议入口统一走访问控制，
    避免旧路由绕过新的课程归属检查。
    """
    return await get_accessible_lecture(db, scope, meeting_id)
