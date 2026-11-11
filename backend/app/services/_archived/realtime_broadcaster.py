"""实时会议 WebSocket 广播器

职责：
1. 管理 meeting_id → WebSocket 连接集合
2. 向同会议所有参与者广播消息（转写片段、状态变更等）
3. 自动清理失效连接

设计权衡：
- 采用内存直连广播而非 Redis Stream pub/sub，简化 MVP 部署依赖
- 单实例足够覆盖中小规模实时会议；多实例扩展时再引入 Redis Stream
"""

import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class RealtimeBroadcaster:
    """WebSocket 连接管理 + 同会议广播"""

    def __init__(self) -> None:
        # meeting_id → set[WebSocket]
        self._connections: dict[str, set[WebSocket]] = {}

    async def subscribe(self, meeting_id: str, ws: WebSocket) -> None:
        """订阅指定会议的消息广播"""
        if meeting_id not in self._connections:
            self._connections[meeting_id] = set()
        self._connections[meeting_id].add(ws)
        logger.info(f"[Broadcaster] 订阅 meeting={meeting_id}, 当前连接数={len(self._connections[meeting_id])}")

    async def unsubscribe(self, meeting_id: str, ws: WebSocket) -> None:
        """取消订阅"""
        conns = self._connections.get(meeting_id)
        if not conns:
            return
        conns.discard(ws)
        if not conns:
            del self._connections[meeting_id]
        logger.info(f"[Broadcaster] 取消订阅 meeting={meeting_id}, 剩余连接数={len(conns) if conns else 0}")

    async def broadcast(self, meeting_id: str, message: dict[str, Any]) -> None:
        """广播消息给同会议所有连接；自动清理失效连接"""
        conns = self._connections.get(meeting_id)
        if not conns:
            return
        text = json.dumps(message, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(text)
            except Exception as e:
                logger.debug(f"[Broadcaster] 发送失败，标记移除: {e}")
                dead.append(ws)
        for ws in dead:
            conns.discard(ws)
        if not conns:
            del self._connections[meeting_id]

    def participant_count(self, meeting_id: str) -> int:
        return len(self._connections.get(meeting_id, set()))


# 全局实例
broadcaster = RealtimeBroadcaster()
