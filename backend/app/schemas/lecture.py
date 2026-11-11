"""课时响应 Schema"""

import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


class AudioAssetSummary(BaseModel):
    id: uuid.UUID
    mime: Optional[str] = None
    duration_ms: Optional[int] = None
    is_demo: bool = False
    version: int = 1

    model_config = {"from_attributes": True}


class TranscriptVersionSummary(BaseModel):
    id: uuid.UUID
    version: int
    status: str
    provider: Optional[str] = None
    segment_count: int = 0


class LectureListItem(BaseModel):
    """课时列表项（供课程范围选择）"""

    id: uuid.UUID
    course_id: uuid.UUID
    title: str
    status: str

    model_config = {"from_attributes": True}


class LectureResponse(BaseModel):
    id: uuid.UUID
    course_id: Optional[uuid.UUID] = None
    title: str
    description: Optional[str] = None
    lecture_date: Optional[date] = None
    status: str
    legacy_meeting: bool = False
    active_audio: Optional[AudioAssetSummary] = None
    active_transcript: Optional[TranscriptVersionSummary] = None

    model_config = {"from_attributes": True}


class TranscriptSegmentResponse(BaseModel):
    """课时当前有效转写片段（按顺序，含毫秒定位，供点选回听定位）"""

    id: uuid.UUID
    version_id: uuid.UUID
    seq_index: int
    speaker: Optional[str] = None
    content: str
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None

    model_config = {"from_attributes": True}
