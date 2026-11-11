"""课程与课时相关 Pydantic Schema"""

import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class CourseCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    teacher: Optional[str] = None
    term: Optional[str] = None


class CourseResponse(BaseModel):
    id: uuid.UUID
    title: str
    teacher: Optional[str] = None
    term: Optional[str] = None
    archived_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CourseListItem(CourseResponse):
    lecture_count: int = 0


class CourseListResponse(BaseModel):
    items: list[CourseListItem]
    total: int
    page: int
    page_size: int


class LectureCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    lecture_date: Optional[date] = None
