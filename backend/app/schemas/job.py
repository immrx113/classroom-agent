"""任务相关 Schema"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class UploadAudioResponse(BaseModel):
    asset_id: uuid.UUID
    job_id: uuid.UUID


class JobResponse(BaseModel):
    id: uuid.UUID
    type: str
    resource_id: uuid.UUID
    input_version: uuid.UUID
    status: str
    stage: str
    attempt: int
    cancel_requested: bool
    error_code: Optional[str] = None
    error_type: Optional[str] = None
    retryable: bool = False
    provider_task_id: Optional[str] = None
    output_version: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime
    # 只读可观测字段（CA-24）：等待原因 / 下次重试时间 / 最近成功阶段 /
    # 已完成/总单元 / 恢复次数；不暴露签名 URL、内部路径或跨主体预算。
    wait_reason: Optional[str] = None
    retry_at: Optional[datetime] = None
    last_successful_stage: Optional[str] = None
    completed_units: int = 0
    total_units: int = 0
    resume_count: int = 0
    # 可读状态文案：unknown（provider_unknown）明确显示“调用结果待核对”
    status_display: str = ""

    model_config = {"from_attributes": True}
