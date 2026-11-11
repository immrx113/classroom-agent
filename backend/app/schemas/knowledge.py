"""知识库相关 Schema"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class KnowledgeDocumentResponse(BaseModel):
    """知识文档响应

    ORM 模型字段名为 metadata_（避开 SQLAlchemy 保留字 metadata），
    通过 alias="metadata_" 让 from_attributes 读取 ORM 的 metadata_ 属性，
    对外输出时用 by_alias=True 产生 "metadata" 字段名。
    """

    id: uuid.UUID
    title: str
    source_type: str
    source_id: Optional[uuid.UUID] = None
    content: str
    # 用 alias 映射：序列化输出时字段名为 "metadata_"，
    # 但前端期望 "metadata"，所以还需要在 API 层处理
    metadata: Optional[dict] = Field(default=None, validation_alias="metadata_")
    created_at: datetime

    model_config = {"from_attributes": True}


class KnowledgeIndexRequest(BaseModel):
    """文本索引请求"""
    title: str
    content: str
    source_type: str = "uploaded_doc"
    metadata: Optional[dict] = None


class KnowledgeSearchRequest(BaseModel):
    """检索请求"""
    query: str
    top_k: int = 5


class SearchResultItem(BaseModel):
    """单条检索结果"""
    id: str
    content: str
    title: str
    source_type: str
    source_id: Optional[str] = None
    metadata: Optional[dict] = None
    score: float
    rerank_score: Optional[float] = None


class KnowledgeSearchResponse(BaseModel):
    """检索响应"""
    query: str
    results: list[SearchResultItem]
    total: int
