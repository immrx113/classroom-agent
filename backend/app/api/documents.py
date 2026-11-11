"""课件文档 API 路由（第四阶段）

上传 → 解析（真实定位块）→ 文档/版本/块持久化；列表 / 详情 / 解析块 / 按身份删除。
同名文件以稳定 document_id 区分，不按 title 合并或扩散删除。
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.config import settings
from app.schemas.document import (
    DocumentListItem,
    DocumentResponse,
    ParsedBlockResponse,
)
from app.services.access import enforce_writable, get_accessible_course
from app.services.document_service import document_service
from app.services.rate_limit_service import FAMILY_DOCUMENT_INDEX, enforce_user_bucket
from app.services.scope import ScopeContext

router = APIRouter(tags=["课件文档"])


@router.post(
    "/courses/{course_id}/documents", response_model=dict, status_code=201
)
async def upload_document(
    course_id: uuid.UUID,
    file: UploadFile = File(...),
    lecture_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """上传课程课件并解析为定位块；解析失败也持久化为明确状态（不伪装成功）。"""
    # 越权先 404、无副作用；回退只读停写（CA-25）；用户入口令牌桶（文档引起索引）拒绝时无新文档/版本/块（CA-14）。
    await get_accessible_course(db, scope, course_id)
    enforce_writable()
    await enforce_user_bucket(scope, FAMILY_DOCUMENT_INDEX)
    content = await file.read()
    if len(content) > settings.MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="文件过大")
    filename = file.filename or "document"
    doc, version = await document_service.create_document(
        db,
        scope,
        course_id=course_id,
        lecture_id=lecture_id,
        filename=filename,
        content=content,
        mime=file.content_type,
    )
    return {
        "id": doc.id,
        "course_id": doc.course_id,
        "lecture_id": doc.lecture_id,
        "title": doc.title,
        "filename": doc.filename,
        "content_hash": doc.content_hash,
        "version_id": version.id,
        "version": version.version,
        "status": version.status,
        "parse_status": version.parse_status,
        "error_code": version.error_code,
        "block_count": version.block_count,
    }


@router.get("/courses/{course_id}/documents", response_model=list[DocumentListItem])
async def list_documents(
    course_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """按课程列出文档（含最新版本摘要），身份独立不按 title 合并。"""
    return await document_service.list_documents(db, scope, course_id)


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """文档详情（含全部版本摘要）。"""
    return await document_service.get_document(db, scope, document_id)


@router.get("/documents/{document_id}/blocks", response_model=list[ParsedBlockResponse])
async def get_document_blocks(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """文档（最新有效版本）解析块与真实定位。"""
    return await document_service.get_blocks(db, scope, document_id)


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """按文档身份删除：文档/版本/块/索引块同步清除，立即停止召回，不按 title 扩散。"""
    await document_service.delete_document(db, scope, document_id)
    return {"success": True, "message": "文档已删除"}
