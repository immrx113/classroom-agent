"""课时业务逻辑层

课时状态由各阶段任务汇总；此处返回当前有效版本与资产摘要。
所有读取均经统一访问控制（归属 + 未归档 + 范围约束）。
活动指针读取时校验归属与匹配，悬空 / 不一致指针降级为 None，不返回错误来源。
"""

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audio_asset import AudioAsset
from app.models.meeting import Meeting
from app.models.transcript_version import TranscriptVersion, TranscriptSegment
from app.services.access import get_accessible_lecture
from app.services.scope import ScopeContext


class LectureService:
    async def get_lecture(
        self, db: AsyncSession, scope: ScopeContext, lecture_id: uuid.UUID
    ) -> tuple[Meeting, dict[str, Any]]:
        """返回课时 + 资产摘要；经统一访问控制校验权限"""
        lecture = await get_accessible_lecture(db, scope, lecture_id)

        active_audio = None
        active_transcript = None

        # 校验有效音频指针：必须属于当前课时
        if lecture.active_audio_id:
            active_audio = (
                await db.execute(
                    select(AudioAsset).where(
                        AudioAsset.id == lecture.active_audio_id,
                        AudioAsset.lecture_id == lecture.id,
                    )
                )
            ).scalar_one_or_none()

        # 校验有效转写指针：属于当前课时，且其音频与有效音频一致
        if lecture.active_transcript_version_id and active_audio is not None:
            row = (
                await db.execute(
                    select(TranscriptVersion).where(
                        TranscriptVersion.id == lecture.active_transcript_version_id,
                        TranscriptVersion.lecture_id == lecture.id,
                        TranscriptVersion.audio_asset_id == lecture.active_audio_id,
                    )
                )
            ).scalar_one_or_none()
            if row:
                seg_count = (
                    await db.execute(
                        select(func.count(TranscriptSegment.id)).where(
                            TranscriptSegment.version_id == row.id
                        )
                    )
                ).scalar_one()
                active_transcript = {
                    "id": row.id,
                    "version": row.version,
                    "status": row.status,
                    "provider": row.provider,
                    "segment_count": seg_count,
                }

        return lecture, {
            "active_audio": active_audio,
            "active_transcript": active_transcript,
        }

    async def list_segments(
        self, db: AsyncSession, scope: ScopeContext, lecture_id: uuid.UUID
    ) -> list[TranscriptSegment]:
        """返回课时当前有效转写版本的片段（按 seq_index 升序，供点选回听定位）。

        仅当有效转写指针指向本课时且其音频与有效音频一致时返回片段；
        悬空 / 不一致指针降级为空列表，不返回错误来源。
        """
        lecture = await get_accessible_lecture(db, scope, lecture_id)
        if lecture.active_transcript_version_id is None:
            return []
        version = (
            await db.execute(
                select(TranscriptVersion).where(
                    TranscriptVersion.id == lecture.active_transcript_version_id,
                    TranscriptVersion.lecture_id == lecture.id,
                    TranscriptVersion.audio_asset_id == lecture.active_audio_id,
                )
            )
        ).scalar_one_or_none()
        if version is None:
            return []
        segments = (
            await db.execute(
                select(TranscriptSegment)
                .where(TranscriptSegment.version_id == version.id)
                .order_by(TranscriptSegment.seq_index.asc())
            )
        ).scalars().all()
        return list(segments)


# 全局实例
lecture_service = LectureService()
