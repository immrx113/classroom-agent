"""课件文档相关 Schema（第四阶段）"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DocumentVersionSummary(BaseModel):
    id: uuid.UUID
    version: int
    status: str
    parse_status: str
    error_code: Optional[str] = None
    block_count: int = 0


class DocumentListItem(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    lecture_id: Optional[uuid.UUID] = None
    title: str
    filename: Optional[str] = None
    mime: Optional[str] = None
    content_hash: Optional[str] = None
    created_at: datetime
    latest_version: Optional[DocumentVersionSummary] = None


class DocumentResponse(DocumentListItem):
    versions: list[DocumentVersionSummary] = []


class ParsedBlockResponse(BaseModel):
    block_id: str
    kind: str
    text: str
    heading: Optional[str] = None
    page_no: Optional[int] = None
    paragraph_index: Optional[int] = None
    table_index: Optional[int] = None
