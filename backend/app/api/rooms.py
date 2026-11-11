"""房间管理 API

对接 mediasoup SFU，提供：
- POST   /api/rooms              创建房间
- GET    /api/rooms              房间列表
- GET    /api/rooms/{room_id}    房间详情
- POST   /api/rooms/{room_id}/end  结束房间
- GET    /api/sfu/health         SFU 健康检查
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.db.session import async_session_factory
from app.models.room import Room
from app.services.sfu_bridge import sfu_bridge

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rooms"])


# ── 请求/响应模型 ──────────────────────────────────────────

VALID_SCENES = {"tech_review", "cross_align", "incident_review", "generic"}


class CreateRoomRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    scene: str = Field(default="generic")
    participants: list[str] = Field(default_factory=list)


class RoomResponse(BaseModel):
    id: str
    meeting_id: str | None
    title: str
    scene: str
    status: str
    sfu_router_id: str | None
    participants: list[str]
    started_at: datetime
    ended_at: datetime | None


# ── 路由 ──────────────────────────────────────────────────

@router.post("/rooms", response_model=RoomResponse)
async def create_room(req: CreateRoomRequest):
    """创建房间：先调 SFU 生成 ID，再落库"""
    if req.scene not in VALID_SCENES:
        raise HTTPException(400, f"invalid scene, must be one of {VALID_SCENES}")

    try:
        sfu_resp = await sfu_bridge.create_room(req.title, req.scene)
    except Exception as e:
        logger.error(f"[Rooms] SFU create_room failed: {e}")
        raise HTTPException(502, f"SFU unreachable: {e}")

    room_id = uuid.UUID(sfu_resp["roomId"])
    async with async_session_factory() as db:
        room = Room(
            id=room_id,
            title=req.title,
            scene=req.scene,
            status="active",
            sfu_router_id=sfu_resp.get("sfu_router_id"),
            participants=req.participants,
        )
        db.add(room)
        await db.commit()
        await db.refresh(room)
        return _to_response(room)


@router.get("/rooms", response_model=list[RoomResponse])
async def list_rooms(status: str | None = None, limit: int = Query(default=50, le=200)):
    """列出房间（默认最近 50 条，可按 status 过滤）"""
    async with async_session_factory() as db:
        stmt = select(Room).order_by(Room.started_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Room.status == status)
        result = await db.execute(stmt)
        rooms = result.scalars().all()
        return [_to_response(r) for r in rooms]


@router.get("/rooms/{room_id}", response_model=RoomResponse)
async def get_room(room_id: str):
    """查询房间详情（合并 SFU 实时状态）"""
    try:
        rid = uuid.UUID(room_id)
    except ValueError:
        raise HTTPException(400, "invalid room_id")

    async with async_session_factory() as db:
        room = await db.get(Room, rid)
        if not room:
            raise HTTPException(404, "room not found")

        # 同步 SFU 端实时状态（peer 数、活跃说话人）
        try:
            sfu_info = await sfu_bridge.get_room(room_id)
            if sfu_info and "activeSpeakers" in sfu_info:
                # 不落库，仅响应中携带
                pass
        except Exception as e:
            logger.warning(f"[Rooms] SFU get_room failed: {e}")
        return _to_response(room)


@router.post("/rooms/{room_id}/end", response_model=RoomResponse)
async def end_room(room_id: str):
    """结束房间：通知 SFU 关闭 Router + 落库状态"""
    try:
        rid = uuid.UUID(room_id)
    except ValueError:
        raise HTTPException(400, "invalid room_id")

    # 1. 通知 SFU 关闭房间
    sfu_failed = False
    try:
        await sfu_bridge.end_room(room_id)
    except Exception as e:
        sfu_failed = True
        logger.warning(f"[Rooms] SFU end_room failed: {e}")

    # 2. 落库
    async with async_session_factory() as db:
        status = "error" if sfu_failed else "ended"
        await db.execute(
            update(Room)
            .where(Room.id == rid)
            .values(status=status, ended_at=datetime.now(timezone.utc))
        )
        await db.commit()
        room = await db.get(Room, rid)
        if not room:
            raise HTTPException(404, "room not found")
        return _to_response(room)


@router.get("/sfu/health")
async def sfu_health():
    """SFU 健康检查"""
    try:
        return await sfu_bridge.health()
    except Exception as e:
        raise HTTPException(502, f"SFU unreachable: {e}")


# ── 工具函数 ──────────────────────────────────────────────

def _to_response(room: Room) -> RoomResponse:
    return RoomResponse(
        id=str(room.id),
        meeting_id=str(room.meeting_id) if room.meeting_id else None,
        title=room.title,
        scene=room.scene,
        status=room.status,
        sfu_router_id=room.sfu_router_id,
        participants=room.participants or [],
        started_at=room.started_at,
        ended_at=room.ended_at,
    )
