"""证据 API 路由"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.schemas.evidence import EvidenceResponse
from app.services.evidence_service import evidence_service
from app.services.scope import ScopeContext

router = APIRouter(prefix="/evidence", tags=["证据"])


@router.get("/{evidence_id}", response_model=EvidenceResponse)
async def get_evidence(
    evidence_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
) -> EvidenceResponse:
    """权限验证后返回原文、定位和来源版本状态"""
    result = await evidence_service.get_evidence(db, scope, evidence_id)
    return EvidenceResponse(**result)
