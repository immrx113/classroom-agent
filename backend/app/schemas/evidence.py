"""证据响应 Schema"""

import uuid
from typing import Optional

from pydantic import BaseModel


class EvidenceResponse(BaseModel):
    id: uuid.UUID
    source_type: str
    source_version_status: str
    quote: Optional[str] = None
    is_valid: bool = True

    # 可稳定解析的来源身份（对应 R8）
    source_version_id: Optional[uuid.UUID] = None   # 固定版本 ID（转写版本 / 文档版本）
    source_version: Optional[int] = None            # 版本号
    source_lecture_id: Optional[uuid.UUID] = None   # 资源（课时）ID
    source_audio_asset_id: Optional[uuid.UUID] = None  # 媒体定位
    source_document_id: Optional[uuid.UUID] = None  # 文档定位
    source_course_id: Optional[uuid.UUID] = None    # 课程归属
    is_current: bool = False                        # 是否当前有效版本

    # 录音来源定位
    segment_start: Optional[int] = None
    segment_end: Optional[int] = None
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None

    # 文档来源定位
    block_id: Optional[str] = None
    page_no: Optional[int] = None

    # 解析后的原文（录音来源为片段文本列表，文档来源首版为 None）
    content: Optional[list[str]] = None
