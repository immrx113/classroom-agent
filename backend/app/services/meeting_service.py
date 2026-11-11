"""会议业务逻辑层"""

import os
import shutil
import uuid
import logging
from typing import Optional

from sqlalchemy import select, func, update, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.meeting import Meeting
from app.models.transcript import Transcript
from app.schemas.meeting import MeetingCreate, MeetingUpdate

logger = logging.getLogger(__name__)


class MeetingService:
    """会议 CRUD + 音频上传 + 转写触发"""

    async def create_meeting(
        self, db: AsyncSession, data: MeetingCreate
    ) -> Meeting:
        """创建会议"""
        meeting = Meeting(
            title=data.title,
            description=data.description,
            participants=data.participants,
            start_time=data.start_time,
            end_time=data.end_time,
        )
        db.add(meeting)
        await db.flush()
        await db.refresh(meeting)
        return meeting

    async def get_meeting(self, db: AsyncSession, meeting_id: uuid.UUID) -> Optional[Meeting]:
        """获取单个会议"""
        result = await db.execute(
            select(Meeting).where(Meeting.id == meeting_id)
        )
        return result.scalar_one_or_none()

    async def list_meetings(
        self, db: AsyncSession, skip: int = 0, limit: int = 20
    ) -> tuple[list[Meeting], int]:
        """获取会议列表 + 总数"""
        # 列表
        result = await db.execute(
            select(Meeting)
            .order_by(Meeting.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        meetings = list(result.scalars().all())

        # 总数
        count_result = await db.execute(select(func.count(Meeting.id)))
        total = count_result.scalar_one()

        return meetings, total

    async def update_meeting(
        self, db: AsyncSession, meeting_id: uuid.UUID, data: MeetingUpdate
    ) -> Optional[Meeting]:
        """更新会议"""
        meeting = await self.get_meeting(db, meeting_id)
        if not meeting:
            return None

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(meeting, key, value)

        await db.flush()
        await db.refresh(meeting)
        return meeting

    async def delete_meeting(self, db: AsyncSession, meeting_id: uuid.UUID) -> bool:
        """删除会议及其所有关联数据"""
        meeting = await self.get_meeting(db, meeting_id)
        if not meeting:
            return False

        # 删除关联音频文件
        if meeting.audio_url and os.path.exists(meeting.audio_url):
            try:
                shutil.rmtree(os.path.dirname(meeting.audio_url), ignore_errors=True)
            except OSError as e:
                logger.warning(f"删除音频文件失败: {e}")

        # 清理产物端证据关联（G4）：先于产物删除，避免删除课时后残留悬空证据关联。
        # 覆盖已支持的产物删除路径：纪要 / 决策 / 笔记版本（笔记由 meeting CASCADE 删除）。
        await db.execute(
            text(
                "DELETE FROM artifact_evidence "
                "WHERE (artifact_type = 'summary' AND artifact_id IN "
                "       (SELECT id FROM summaries WHERE meeting_id = :mid)) "
                "   OR (artifact_type = 'decision' AND artifact_id IN "
                "       (SELECT id FROM decisions WHERE meeting_id = :mid)) "
                "   OR (artifact_type = 'note_version' AND artifact_id IN "
                "       (SELECT id FROM note_versions WHERE lecture_id = :mid))"
            ),
            {"mid": meeting_id},
        )

        # 手动删除关联数据（避免外键约束不一致）
        for table in ("agent_runs", "decisions", "transcripts", "summaries",
                       "action_items", "risks", "chat_sessions", "rooms"):
            await db.execute(
                text(f"DELETE FROM {table} WHERE meeting_id = :mid"),
                {"mid": meeting_id},
            )
        # 知识文档通过 source_id 关联
        await db.execute(
            text("DELETE FROM knowledge_documents WHERE source_id = :mid"),
            {"mid": meeting_id},
        )

        await db.delete(meeting)
        await db.flush()
        return True

    async def save_audio(
        self, db: AsyncSession, meeting_id: uuid.UUID, file_content: bytes, filename: str
    ) -> Optional[Meeting]:
        """保存音频文件并更新会议"""
        meeting = await self.get_meeting(db, meeting_id)
        if not meeting:
            return None

        # 确保上传目录存在
        meeting_upload_dir = os.path.join(settings.UPLOAD_DIR, str(meeting_id))
        os.makedirs(meeting_upload_dir, exist_ok=True)

        # 保存文件
        file_path = os.path.join(meeting_upload_dir, filename)
        with open(file_path, "wb") as f:
            f.write(file_content)

        # 更新会议记录
        meeting.audio_url = file_path
        meeting.status = "pending"
        await db.flush()
        await db.refresh(meeting)

        logger.info(f"音频已保存: {file_path}")
        return meeting

    async def get_transcripts(
        self, db: AsyncSession, meeting_id: uuid.UUID
    ) -> list[Transcript]:
        """获取会议的转写记录"""
        result = await db.execute(
            select(Transcript)
            .where(Transcript.meeting_id == meeting_id)
            .order_by(Transcript.seq_index)
        )
        return list(result.scalars().all())

    async def get_transcript_count(
        self, db: AsyncSession, meeting_id: uuid.UUID
    ) -> int:
        """获取转写记录数量"""
        result = await db.execute(
            select(func.count(Transcript.id)).where(Transcript.meeting_id == meeting_id)
        )
        return result.scalar_one()


# 全局实例
meeting_service = MeetingService()
