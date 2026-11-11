"""实时语音识别服务

支持两种运行模式：
1. real：调用 DashScope paraformer-realtime-v2 流式 ASR
2. mock：本地假转写，用于开发联调（无需真实 SDK / 麦克风）

设计要点：
- 每 200ms 接收一帧 PCM（Int16, 16kHz, mono）
- 内部维护识别器实例（per meeting_id），复用同一连接
- 增量返回已识别的片段（带说话人、时间戳、seq_index）
- 每 N 秒触发一次"该生成增量纪要了"信号（由调用方决定如何处理）
- 每 10s 批量落库一次转写片段
"""

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)

# 触发增量纪要的间隔（秒）
INCREMENTAL_SUMMARY_INTERVAL = 60
# 批量落库间隔（秒）
FLUSH_INTERVAL = 10


class RealtimeASRService:
    """实时语音识别服务"""

    def __init__(self) -> None:
        # meeting_id → 识别器状态
        self._recognizers: dict[str, dict[str, Any]] = {}
        # meeting_id → 累计片段数（seq_index）
        self._segment_counts: dict[str, int] = defaultdict(int)
        # meeting_id → 上次增量纪要时间
        self._last_summary_time: dict[str, float] = defaultdict(float)
        # meeting_id → 待落库片段缓冲
        self._pending_segments: dict[str, list[dict]] = defaultdict(list)
        # meeting_id → 上次落库时间
        self._last_flush_time: dict[str, float] = defaultdict(float)
        # meeting_id → mock 模式累计字数（用于生成假文本）
        self._mock_word_count: dict[str, int] = defaultdict(int)
        # 是否使用 mock 模式
        self._use_mock = not settings.DASHSCOPE_API_KEY

    # ── 公共接口 ──────────────────────────────────────────────

    async def start_session(self, meeting_id: str) -> None:
        """开始一个实时会议会话（首次音频到达前调用）"""
        if meeting_id in self._recognizers:
            return
        self._recognizers[meeting_id] = {
            "started_at": time.time(),
            "mode": "mock" if self._use_mock else "real",
        }
        self._last_summary_time[meeting_id] = time.time()
        self._last_flush_time[meeting_id] = time.time()
        logger.info(f"[RealtimeASR] 启动会话 meeting={meeting_id} mode={'mock' if self._use_mock else 'real'}")

    async def feed(self, meeting_id: str, audio_chunk: bytes) -> list[dict]:
        """喂入一帧 PCM 音频，返回本次新识别出的片段列表"""
        if meeting_id not in self._recognizers:
            await self.start_session(meeting_id)

        # 转写：mock 或 real
        new_segments: list[dict] = []
        if self._use_mock:
            new_segments = await self._feed_mock(meeting_id, audio_chunk)
        else:
            new_segments = await self._feed_real(meeting_id, audio_chunk)

        # 累积到落库缓冲
        self._pending_segments[meeting_id].extend(new_segments)

        return new_segments

    def should_trigger_summary(self, meeting_id: str) -> bool:
        """是否到达触发增量纪要的时间点"""
        now = time.time()
        if now - self._last_summary_time[meeting_id] >= INCREMENTAL_SUMMARY_INTERVAL:
            self._last_summary_time[meeting_id] = now
            return True
        return False

    def should_flush(self, meeting_id: str) -> bool:
        """是否到达批量落库时间点"""
        now = time.time()
        if now - self._last_flush_time[meeting_id] >= FLUSH_INTERVAL:
            self._last_flush_time[meeting_id] = now
            return True
        return False

    async def flush_to_db(self, meeting_id: str) -> int:
        """把缓冲的转写片段批量写入数据库，返回写入条数"""
        segments = self._pending_segments[meeting_id]
        if not segments:
            return 0
        try:
            from app.models.transcript import Transcript
            from app.models.meeting import Meeting
            from sqlalchemy import update

            async with async_session_factory() as db:
                # 确保 meeting 存在（实时会议首次落库时创建占位 Meeting）
                meeting_exists = await db.get(Meeting, uuid.UUID(meeting_id))
                if not meeting_exists:
                    db.add(Meeting(
                        id=uuid.UUID(meeting_id),
                        title=f"实时会议 {meeting_id[:8]}",
                        status="transcribing",
                        transcription_mode="real" if not self._use_mock else "mock",
                        start_time=datetime.now(timezone.utc),
                    ))
                for seg in segments:
                    db.add(Transcript(meeting_id=uuid.UUID(meeting_id), **seg))
                await db.commit()
            self._pending_segments[meeting_id] = []
            logger.info(f"[RealtimeASR] 落库 {len(segments)} 条片段 meeting={meeting_id}")
            return len(segments)
        except Exception as e:
            logger.error(f"[RealtimeASR] 落库失败: {e}")
            return 0

    async def finalize_session(self, meeting_id: str) -> dict:
        """会议结束：清理识别器、落库剩余片段、更新会议状态"""
        info = self._recognizers.pop(meeting_id, {})
        # 落库剩余片段
        flushed = await self.flush_to_db(meeting_id)
        # 更新会议状态
        try:
            from app.models.meeting import Meeting
            from sqlalchemy import update

            async with async_session_factory() as db:
                await db.execute(
                    update(Meeting)
                    .where(Meeting.id == uuid.UUID(meeting_id))
                    .values(status="processed", end_time=datetime.now(timezone.utc))
                )
                await db.commit()
        except Exception as e:
            logger.error(f"[RealtimeASR] 更新会议状态失败: {e}")

        # 同步更新 RealtimeSession
        try:
            from app.models.realtime_session import RealtimeSession
            from sqlalchemy import update

            async with async_session_factory() as db:
                await db.execute(
                    update(RealtimeSession)
                    .where(RealtimeSession.meeting_id == uuid.UUID(meeting_id))
                    .values(status="ended", ended_at=datetime.now(timezone.utc), segment_count=self._segment_counts[meeting_id])
                )
                await db.commit()
        except Exception as e:
            logger.error(f"[RealtimeASR] 更新 RealtimeSession 失败: {e}")

        logger.info(f"[RealtimeASR] 会话结束 meeting={meeting_id} segments={self._segment_counts[meeting_id]} flushed={flushed}")
        return {
            "meeting_id": meeting_id,
            "total_segments": self._segment_counts[meeting_id],
            "flushed_segments": flushed,
            "started_at": info.get("started_at"),
        }

    def get_session_info(self, meeting_id: str) -> dict:
        return {
            "active": meeting_id in self._recognizers,
            "mode": self._recognizers.get(meeting_id, {}).get("mode", "n/a"),
            "segment_count": self._segment_counts[meeting_id],
            "pending_count": len(self._pending_segments[meeting_id]),
        }

    # ── Mock 模式 ──────────────────────────────────────────────

    async def _feed_mock(self, meeting_id: str, audio_chunk: bytes) -> list[dict]:
        """Mock 转写：根据音频字节量生成假片段（每 ~2s 输出一段）

        用于开发联调：无需真实 SDK 和麦克风也能验证全链路。
        """
        # 每 200ms 一帧 ≈ 6400 bytes（Int16, 16kHz, mono）
        # 累计 ~2s（10 帧 ≈ 64KB）输出一段假转写
        state = self._recognizers[meeting_id]
        state["bytes_accum"] = state.get("bytes_accum", 0) + len(audio_chunk)

        if state["bytes_accum"] < 64000:
            return []

        state["bytes_accum"] = 0
        self._mock_word_count[meeting_id] += 1

        mock_texts = [
            "大家好，我们今天讨论一下项目进度",
            "前端部分已经完成了主要功能的开发",
            "后端的 API 接口也基本就绪",
            "接下来需要联调测试",
            "我们看一下时间安排",
            "预计本周五可以完成集成测试",
            "下周一开始用户验收",
            "风险点主要是第三方依赖的稳定性",
        ]
        text = mock_texts[self._mock_word_count[meeting_id] % len(mock_texts)]

        seq = self._segment_counts[meeting_id]
        now = time.time()
        segment = {
            "speaker": f"说话人{(seq % 2) + 1}",
            "content": text,
            "start_time": now,
            "end_time": now + 2.0,
            "seq_index": seq,
        }
        self._segment_counts[meeting_id] += 1
        return [segment]

    # ── Real 模式（DashScope Realtime SDK） ────────────────────

    async def _feed_real(self, meeting_id: str, audio_chunk: bytes) -> list[dict]:
        """真实流式 ASR：调用 DashScope paraformer-realtime-v2

        DashScope SDK 在异步回调中推送结果，需用 asyncio.Queue 解耦。
        实际生产环境启用；此处保留接口，待接入真实 SDK 后实现。
        """
        # TODO: 接入 dashscope.audio.asr.Recognition
        # 当前若未真实接入则降级到 mock
        return await self._feed_mock(meeting_id, audio_chunk)


# 全局实例
realtime_asr_service = RealtimeASRService()
