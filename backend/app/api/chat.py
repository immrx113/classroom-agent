"""对话 API：会话管理 + SSE 流式对话"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.services.access import get_accessible_lecture
from app.services.chat_service import chat_service
from app.services.retrieval_service import retrieval_service
from app.services.scope import ScopeContext

router = APIRouter(prefix="/chat", tags=["chat"])


# ── 请求/响应模型 ──

class CreateSessionRequest(BaseModel):
    meeting_id: str | None = None
    title: str | None = None


class ChatRequest(BaseModel):
    query: str
    images: list[str] | None = None  # base64 data URL 列表，用于多模态对话


class ClassroomChatRequest(BaseModel):
    query: str
    course_id: str | None = None
    lecture_id: str | None = None


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    metadata: dict | None = None
    created_at: str


class SessionResponse(BaseModel):
    id: str
    meeting_id: str | None = None
    title: str | None = None
    created_at: str


# ── 会话归属校验 ──

async def _require_accessible_session(
    db: AsyncSession, scope: ScopeContext, session_id: uuid.UUID
):
    """加载会话并校验归属：全局会话同样按 owner_id 隔离，不因无课时而放宽"""
    session = await chat_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.owner_id != scope.subject_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.meeting_id is not None:
        await get_accessible_lecture(db, scope, session.meeting_id)
    return session


# ── 会话管理 ──

@router.post("/sessions", response_model=SessionResponse)
async def create_session(
    req: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """创建对话会话"""
    meeting_id: uuid.UUID | None = None
    if req.meeting_id:
        try:
            meeting_id = uuid.UUID(req.meeting_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的会议 ID")
        await get_accessible_lecture(db, scope, meeting_id)

    session = await chat_service.create_session(
        db,
        owner_id=scope.subject_id,
        meeting_id=meeting_id,
        title=req.title,
    )
    return SessionResponse(
        id=str(session.id),
        meeting_id=str(session.meeting_id) if session.meeting_id else None,
        title=session.title,
        created_at=session.created_at.isoformat(),
    )


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    meeting_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """获取会话列表（默认仅可访问课时会话 + 全局会话）"""
    mid: uuid.UUID | None = None
    if meeting_id:
        try:
            mid = uuid.UUID(meeting_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的会议 ID")
        await get_accessible_lecture(db, scope, mid)

    sessions = await chat_service.list_sessions(db, meeting_id=mid, scope=scope)
    return [
        SessionResponse(
            id=str(s.id),
            meeting_id=str(s.meeting_id) if s.meeting_id else None,
            title=s.title,
            created_at=s.created_at.isoformat(),
        )
        for s in sessions
    ]


@router.get("/sessions/{session_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """获取会话消息历史"""
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话 ID")

    await _require_accessible_session(db, scope, sid)

    msgs = await chat_service.get_session_messages(db, sid)
    return [
        MessageResponse(
            id=str(m.id),
            session_id=str(m.session_id),
            role=m.role,
            content=m.content,
            metadata=m.metadata_,
            created_at=m.created_at.isoformat(),
        )
        for m in msgs
    ]


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """删除会话"""
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话 ID")

    await _require_accessible_session(db, scope, sid)

    ok = await chat_service.delete_session(db, sid)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "已删除"}


# ── SSE 流式对话 ──

@router.post("/sessions/{session_id}/stream")
async def chat_stream(
    session_id: str,
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """SSE 流式对话

    返回 Server-Sent Events：
    - data: {"type": "token", "content": "..."}  增量内容
    - data: {"type": "done", "sources": [...]}  完成
    - data: {"type": "error", "message": "..."} 错误
    """
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话 ID")

    await _require_accessible_session(db, scope, sid)

    if not req.query.strip():
        raise HTTPException(status_code=400, detail="查询不能为空")

    async def event_generator():
        try:
            async for delta in chat_service.chat_stream(
                db, sid, req.query, req.images, scope
            ):
                yield f"data: {json.dumps({'type': 'token', 'content': delta}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 课堂问答 SSE（R5/A5：范围检索 → 流式答案 + 来源标识） ──

@router.post("/classroom/stream")
async def classroom_chat_stream(
    req: ClassroomChatRequest,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """课堂问答流式（范围课程/课时，检索两路召回 → 融合去重 → 流式答案 + 来源标识）。

    SSE 事件：
    - data: {"type": "sources", "status": ..., "results": [...], "trace": {...}}
    - data: {"type": "token", "content": ...}
    - data: {"type": "done", "status": ...}
    - data: {"type": "error", "message": ...}
    """
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="查询不能为空")

    course_id: uuid.UUID | None = None
    lecture_id: uuid.UUID | None = None
    try:
        if req.course_id:
            course_id = uuid.UUID(req.course_id)
        if req.lecture_id:
            lecture_id = uuid.UUID(req.lecture_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的课程或课时 ID")

    chat_scope = ScopeContext(
        subject_id=scope.subject_id,
        course_id=course_id,
        lecture_id=lecture_id,
    )

    # 预校验范围权限：SSE 内部抛 404/403 会变成 200 的 error 流，先在此按 HTTP 语义拒绝。
    await retrieval_service._normalize_scope(db, chat_scope)

    async def event_generator():
        try:
            async for event in chat_service.classroom_chat_stream(
                db, req.query, chat_scope
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
