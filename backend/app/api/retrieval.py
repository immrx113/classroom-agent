"""检索 API 路由（第四阶段：检索闭环）

范围内两路召回 → 融合/去重/重排 → 预算内上下文 → 统一证据响应。
course_id / lecture_id 为显式范围声明，客户端参数非授权依据。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.schemas.retrieval import RetrievalSearchRequest, RetrievalSearchResponse
from app.services.retrieval_service import retrieval_service
from app.services.scope import ScopeContext

router = APIRouter(prefix="/retrieval", tags=["检索"])


@router.post("/search", response_model=RetrievalSearchResponse)
async def search(
    body: RetrievalSearchRequest,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> RetrievalSearchResponse:
    """范围检索：显式课程/课时范围，返回统一证据（来源/版本/定位/可核对原文）。"""
    search_scope = ScopeContext(
        subject_id=scope.subject_id,
        course_id=body.course_id,
        lecture_id=body.lecture_id,
    )
    result = await retrieval_service.search(
        db, search_scope, body.query, top_k=body.top_k
    )
    return RetrievalSearchResponse(**result)
