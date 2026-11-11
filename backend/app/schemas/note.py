"""笔记相关 Schema"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class OrganizeRequest(BaseModel):
    """发起全课整理：固定要整理的转写版本。"""

    transcript_version_id: uuid.UUID


class EditNoteRequest(BaseModel):
    """编辑草稿：仅可改结构与文本，来源与证据由服务端解析维护。"""

    title: Optional[str] = None
    outline: Optional[list[str]] = None
    sections: Optional[list[dict]] = None
    key_conclusions: Optional[list[dict]] = None
    knowledge_points: Optional[list[dict]] = None
    examples: Optional[list[dict]] = None
    key_points: Optional[list[dict]] = None
    edit_source: Optional[dict] = None


class NoteResponse(BaseModel):
    id: uuid.UUID
    lecture_id: uuid.UUID
    transcript_version_id: Optional[uuid.UUID] = None
    pipeline_version: Optional[str] = None
    status: str
    content: Optional[dict] = None
    previous_version_id: Optional[uuid.UUID] = None
    edited_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}
