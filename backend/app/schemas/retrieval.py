"""检索（第四阶段）相关 Schema"""

import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field


class RetrievalSearchRequest(BaseModel):
    """范围检索请求：course_id / lecture_id 显式声明，客户端参数非授权依据。"""

    query: str = Field(..., min_length=1, max_length=2000)
    course_id: Optional[uuid.UUID] = None
    lecture_id: Optional[uuid.UUID] = None
    top_k: Optional[int] = Field(default=None, ge=1, le=50)


class RetrievalResult(BaseModel):
    source_type: str
    source_version_id: str
    source_version: Optional[int] = None
    source_lecture_id: Optional[str] = None
    source_course_id: Optional[str] = None
    source_document_id: Optional[str] = None
    source_audio_asset_id: Optional[str] = None
    is_current: bool = False

    # 定位（文档块 / 转写片段）
    block_id: Optional[str] = None
    page_no: Optional[int] = None
    heading: Optional[str] = None
    paragraph_index: Optional[int] = None
    table_index: Optional[int] = None
    segment_start: Optional[int] = None
    segment_end: Optional[int] = None
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None

    content: list[str] = []
    quote: str
    score: float
    rerank_score: float = 0.0
    adjacent: list[str] = []
    note_evidence_ids: list[str] = []


class RetrievalSearchResponse(BaseModel):
    query: str
    status: str  # ok / no_evidence / retrieval_error / degraded
    index_status: str  # current / none
    index_version_id: Optional[str] = None
    results: list[RetrievalResult] = []
    total: int = 0
    trace: dict[str, Any] = {}
