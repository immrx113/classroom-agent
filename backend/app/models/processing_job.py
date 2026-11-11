"""持久任务与 outbox 模型

第二阶段「可靠输入」：上传完成后的转写处理以持久任务（processing_jobs）承载，
outbox_events 在任务创建事务内一并写入，保证「提交即投递」至少一次语义。
状态机与恢复见 ``app.services.job_service``。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    String,
    Integer,
    Boolean,
    DateTime,
    ForeignKey,
    func,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        # 任务业务键唯一：同主体/资源/输入/语义配置只允许一个活跃任务（CA-01）
        # 旧行 idempotency_key 为 NULL，PG 唯一约束不冲突。
        Index("uq_processing_jobs_idempotency_key", "idempotency_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    # 资源归属：课时（meetings.id），用于权限检查，不暴露跨主体
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # 固定输入：音频资产 ID（每个任务绑定固定 audio_asset_id）
    input_version: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    config_hash: Mapped[str | None] = mapped_column(String(128))
    # 归属主体（服务端确定，客户端不可自选）；旧行为 NULL 兼容
    subject_id: Mapped[str | None] = mapped_column(String(100))
    # 任务业务键（幂等创建，唯一）；见技术方案 3.1
    idempotency_key: Mapped[str | None] = mapped_column(String(300))
    # 固定输入快照引用 / 语义配置快照 / 策略版本（与语义配置分离）
    input_snapshot_ref: Mapped[str | None] = mapped_column(String(200))
    config_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    policy_version: Mapped[str | None] = mapped_column(String(50))
    # 租约：owner / epoch / 过期时间（CA-11 原子领取与旧执行者隔离）
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 控制等待原因 / 最近成功阶段 / 恢复次数（可观测性）
    wait_reason: Mapped[str | None] = mapped_column(String(50))
    # 累计控制等待秒数（CA-17：累计达 CHECKPOINT_CONTROL_WAIT_SECONDS 后明确失败，不无限等待）
    control_wait_elapsed_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_successful_stage: Mapped[str | None] = mapped_column(String(50))
    resume_count: Mapped[int] = mapped_column(Integer, default=0)
    # Checkpoint 复用次数（CA-24）：恢复时命中已成功产物、跳过外部调用的次数；
    # 与 resume_count（网关恢复）共同构成「实际复用」，区别于成功产物存量。
    reuse_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # queued / running / retry_wait / succeeded / failed / cancelled
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="queued")
    # created / submitting / submitted / polling / parsing / validating / publishing
    stage: Mapped[str] = mapped_column(String(50), nullable=False, default="created")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_type: Mapped[str | None] = mapped_column(String(50))
    # 供应商 task_id：提交后持久化，恢复沿用同一任务，不因超时重复提交
    provider_task_id: Mapped[str | None] = mapped_column(String(200))
    # 成功发布的转写版本 ID（幂等：重复投递不产生第二份有效版本）
    output_version: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("business_key", name="uq_outbox_events_business_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_key: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # pending / dispatched / consumed / failed
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobCheckpoint(Base):
    """阶段级 Checkpoint：已成功持久化产物，恢复时复用而非重新调用外部服务。

    ``step_key`` 固定为阶段名（asr.submit / asr.result / transcript.publish /
    organize.manifest / organize.extract / organize.summary / note.draft /
    index.manifest / index.embed / index.activate），``unit_key`` 在阶段级固定为
    ``stage``，在逐块/逐单元为稳定块序号或稳定单元键。同一 (job_id, step_key, unit_key)
    唯一；输出通过 ``output_ref`` 引用产物事实表，``output_hash`` + ``schema_version``
    校验结果仍有效，引用损坏返回 ``checkpoint_corrupt`` 而非静默重建。
    """

    __tablename__ = "job_checkpoints"
    __table_args__ = (
        UniqueConstraint("job_id", "step_key", "unit_key", name="uq_job_checkpoint_job_step_unit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    step_key: Mapped[str] = mapped_column(String(100), nullable=False)
    unit_key: Mapped[str] = mapped_column(String(200), nullable=False)
    input_hash: Mapped[str | None] = mapped_column(String(128))
    config_hash: Mapped[str | None] = mapped_column(String(128))
    # pending / running / succeeded / failed / unknown
    status: Mapped[str] = mapped_column(String(50), default="pending")
    # 产物引用（JSON 字符串或稳定 ID 文本）与内容摘要
    output_ref: Mapped[str | None] = mapped_column(String(500))
    # 小结构化结果直接落地（汇总/ASR 结果等，避免整段课堂正文进通用日志）
    output_data: Mapped[dict | None] = mapped_column(JSONB)
    output_hash: Mapped[str | None] = mapped_column(String(128))
    schema_version: Mapped[str | None] = mapped_column(String(50))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProviderCall(Base):
    """每次实际外发供应商操作的持久记录（S1 用于调用计数，S3 接入网关/预算）。

    ``attempt_no`` 为该 operation 的实际请求次数（首次含在内最多 3 次）；
    记录输入摘要、供应商回执/任务 ID、错误分类与计量状态，不含密钥、正文或签名 URL。
    """

    __tablename__ = "provider_calls"
    __table_args__ = (
        UniqueConstraint("operation_key", "attempt_no", name="uq_provider_calls_op_attempt"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    operation_key: Mapped[str] = mapped_column(String(300), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("processing_jobs.id", ondelete="SET NULL")
    )
    checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # 归属主体（服务端确定，客户端不可自选）：IndexIntent 等无 job 的调用也能审计关联（CA-24）
    subject_id: Mapped[str | None] = mapped_column(String(100), index=True)
    provider: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))
    operation_type: Mapped[str | None] = mapped_column(String(50))
    input_hash: Mapped[str | None] = mapped_column(String(128))
    provider_task_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str | None] = mapped_column(String(50))
    error_code: Mapped[str | None] = mapped_column(String(100))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 成功结果的持久化引用（JSONB）：成功提交至业务 Checkpoint 前失联时，恢复可复用结果，
    # 不因 Checkpoint 未写而重新外发/收费（CA-07）。
    result_ref: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    reserved_amount: Mapped[int | None] = mapped_column(Integer)
    settled_amount: Mapped[int | None] = mapped_column(Integer)
    usage_state: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
