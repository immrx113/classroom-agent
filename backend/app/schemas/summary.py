"""纪要相关 Schema"""

import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


# ── 纪要 ──

class SummaryListItemResponse(BaseModel):
    """纪要列表项"""
    id: str
    meeting_id: str
    meeting_title: str
    content: str
    status: str
    created_at: datetime


class SummaryResponse(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    content: str
    key_points: Optional[list[str]] = None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── 行动项 ──

class ActionItemResponse(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    title: str
    assignee: Optional[str] = None
    due_date: Optional[date] = None
    priority: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ActionItemUpdate(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee: Optional[str] = None


# ── 风险 ──

class RiskResponse(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    description: str
    severity: str
    mitigation: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── 综合响应 ──

class MeetingSummaryResponse(BaseModel):
    """纪要 + 行动项 + 风险综合响应"""
    summary: Optional[SummaryResponse] = None
    action_items: list[ActionItemResponse] = []
    risks: list[RiskResponse] = []
