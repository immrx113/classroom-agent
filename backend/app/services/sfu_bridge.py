"""SFU 桥接服务

封装对 mediasoup SFU 服务（Node.js, port 4001）的 HTTP 调用。
Python 后端不直接处理 WebRTC 媒体流，只做房间管理 + 元数据落库。
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class SFUBridge:
    """SFU HTTP 客户端"""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        # trust_env=False 禁用系统代理读取，避免本地代理转发导致 502
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=10.0, trust_env=False,
        )

    async def health(self) -> dict[str, Any]:
        """SFU 健康检查"""
        r = await self._client.get("/health")
        r.raise_for_status()
        return r.json()

    async def create_room(self, title: str, scene: str) -> dict[str, Any]:
        """创建房间（SFU 端只生成 ID，Router 在首个客户端 join 时 lazy 创建）"""
        r = await self._client.post(
            "/rooms",
            json={"title": title, "scene": scene},
        )
        r.raise_for_status()
        return r.json()

    async def get_room(self, room_id: str) -> dict[str, Any] | None:
        """查询房间状态（房间不存在或空时返回 None）"""
        r = await self._client.get(f"/rooms/{room_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def end_room(self, room_id: str) -> None:
        """结束房间（关闭 mediasoup Router）"""
        r = await self._client.post(f"/rooms/{room_id}/end")
        r.raise_for_status()

    async def aclose(self) -> None:
        """关闭 httpx 客户端，释放连接池资源"""
        await self._client.aclose()


sfu_bridge = SFUBridge(settings.SFU_BASE_URL)
