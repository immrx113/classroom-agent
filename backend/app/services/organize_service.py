"""全课整理服务（第三阶段）

流程：固定转写版本与配置 → 分块清单（核心覆盖全量 + 相邻重叠上下文）
→ 逐块抽取 → 去重 / 衔接 → 汇总章节笔记 → 结构 / 证据 / 覆盖校验
→ 草稿 / 待确认 / 条件发布 → 索引交接意向。

幂等与恢复：
- 任务 ``output_version`` 指向本任务产出的笔记版本，重复投递 / 重试不重复产物；
- 逐块提交状态与输出，worker 失联后恢复沿用已成功块，不重复调用；
- 发布为条件切换：晚完成的旧输入整理不覆盖已发布的新输入笔记（P03-11）。
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.audio_asset import AudioAsset
from app.models.evidence import ArtifactEvidence, EvidenceSpan
from app.models.meeting import Meeting
from app.models.note_version import NoteVersion
from app.models.organize import ChunkManifest, ChunkItem, IndexIntent
from app.models.processing_job import ProcessingJob, ProviderCall, OutboxEvent, JobCheckpoint
from app.models.transcript_version import TranscriptVersion, TranscriptSegment
from app.services import lease_service
from app.services import rate_limit_service
from app.services.ai_call_gateway import ControlWaitError, json_deserializer, json_serializer
from app.services.access import get_accessible_lecture
from app.services.checkpoint_service import (
    CHECKPOINT_SCHEMA_VERSION,
    PIPELINE_VERSION,
    CheckpointCorrupt,
    checkpoint_intact,
    compute_idempotency_key,
    compute_semantic_hash,
    get_checkpoint,
    mark_checkpoint_reused,
    mark_checkpoint_succeeded,
    norm_json,
    policy_version,
    sha256_hex,
)
from app.services.errors import (
    ERR_INVALID_RESULT,
    BudgetExhaustedError,
    CallCancelled,
    ProviderUnknownError,
    ServiceError,
)
from app.services.organize_model import ChunkInput, ModelError, WorkerCrash, get_organize_model
from app.services.scope import ScopeContext

logger = logging.getLogger(__name__)

# 测试注入：发布切换提交前抛异常（P03-11 原子发布）。生产恒为 None。
_publish_hook = None
# 测试注入：汇总 Checkpoint 提交后抛异常（CA-04 汇总恢复）。生产恒为 None。
_post_summary_hook = None

# 笔记状态：draft / pending / published / superseded
# 结论确认状态：confirmed / pending
# 结论来源：teacher_explicit / material_derived / model_supplement

_ORIGINS = {"teacher_explicit", "material_derived", "model_supplement"}
_STATUSES = {"confirmed", "pending"}

# 四类课堂整理分支（R3）：笔记 / 知识点 / 例题 / 重点难点。顺序稳定，供调度与运行详情。
BRANCH_NAMES = ("note", "knowledge_point", "example", "key_point")


def _selected_branch_names(plan: dict | None) -> set[str]:
    """由规划决定本次调度选中的分支（A3 调度约定）。

    无规划时默认四类全选（与 ``OrganizeModel.plan`` 基类一致）。未选中的分支不产出、
    不执行，记录为「跳过」；选中但无材料的分支为「执行后产物为空」。
    """
    branches = (plan or {}).get("branches") or {}
    if not branches:
        return set(BRANCH_NAMES)
    return {
        name for name in BRANCH_NAMES
        if branches.get(name, {}).get("selected", False)
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


# A4 返工 v3：四类条目进入正式检索的正文字段（与 index_service._note_text 对齐）。
# 发布前用它识别「只改正文、保留原引文和证据」的实质变化——旧引文仍存在不能单独作为
# 新正文继续 confirmed 的依据；正文已变更且未重新核实的条目回到 pending。
_BODY_FIELDS = {
    "key_conclusions": ("text",),
    "knowledge_points": ("definition", "applicability"),
    "examples": ("problem", "derivation"),
    "key_points": ("point", "reason"),
}


def _entry_body_signature(entry: dict, list_key: str) -> str:
    """正文签名：条目在创建时固化的正文内容指纹（含会进入正式检索的正文字段）。"""
    fields = _BODY_FIELDS[list_key]
    return "\x1f".join((((entry.get(k) or "") or "").strip()) for k in fields)


# A4 返工 v4：各条目列表的「锚点键」（来源解析/去重/跨编辑基线匹配的稳定身份之一）。
_ANCHOR_FIELDS = {
    "key_conclusions": "text",
    "knowledge_points": "definition",
    "examples": "problem",
    "key_points": "point",
}


# 整理提示词版本：提示词/输出 schema 变化即视为语义变化，纳入任务身份（CA-01）。
ORGANIZE_PROMPT_VERSION = "organize-v1"


def _organize_config_snapshot() -> dict:
    """语义配置快照（影响产物的字段），与策略版本分离（CA-01/CA-02）。

    不含重试参数（ORGANIZE_MAX_ATTEMPTS 等留 policy_version）；含 prompt/schema 版本。
    """
    return {
        "context_budget": settings.ORGANIZE_CONTEXT_BUDGET,
        "prompt_reserve": settings.ORGANIZE_PROMPT_RESERVE,
        "output_reserve": settings.ORGANIZE_OUTPUT_RESERVE,
        "chunk_overlap": settings.ORGANIZE_CHUNK_OVERLAP,
        "model": settings.ORGANIZE_MODEL,
        "prompt_version": ORGANIZE_PROMPT_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
    }


def _organize_config_hash() -> str:
    """语义配置身份：由规范化语义快照统一计算（与 config_snapshot 一致）。"""
    return compute_semantic_hash(_organize_config_snapshot())


def _organize_input_snapshot_hash(transcript_version_id: uuid.UUID) -> str:
    """整理固定输入身份：转写版本即有序片段清单的稳定引用。"""
    return sha256_hex(f"transcript_version:{transcript_version_id}")


class OrganizeCancelled(Exception):
    pass


async def _load_segments(db: AsyncSession, version_id: uuid.UUID) -> list[dict]:
    rows = (
        await db.execute(
            select(TranscriptSegment)
            .where(TranscriptSegment.version_id == version_id)
            .order_by(TranscriptSegment.seq_index)
        )
    ).scalars().all()
    return [
        {
            "seq_index": s.seq_index,
            "speaker": s.speaker,
            "content": s.content,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
        }
        for s in rows
    ]


async def _check_cancel(db: AsyncSession, job: ProcessingJob) -> None:
    await db.refresh(job)
    if job.cancel_requested:
        raise OrganizeCancelled()


class OrganizeService:
    # ── 任务创建 ──────────────────────────────────────────────────────
    async def create_organize_job(
        self,
        db: AsyncSession,
        scope: ScopeContext,
        *,
        lecture_id: uuid.UUID,
        transcript_version_id: uuid.UUID,
    ) -> ProcessingJob:
        await get_accessible_lecture(db, scope, lecture_id)

        version = await db.get(TranscriptVersion, transcript_version_id)
        if version is None or version.lecture_id != lecture_id:
            raise ServiceError(404, "invalid_input", "转写版本不存在或不属于该课时", retryable=False)

        # 显式演示资产不能作为正式整理来源（P03-02）
        asset = await db.get(AudioAsset, version.audio_asset_id)
        if asset is not None and asset.is_demo:
            raise ServiceError(422, "invalid_input", "演示资产不能作为正式整理来源", retryable=False)

        # 任务身份（CA-01）：同主体/资源/输入/语义配置只允许一个任务。
        # 语义配置变化（config_hash）或输入变化（transcript_version）建独立任务；
        # 仅调整重试策略（policy_version）不改变业务键，不误废弃已成功语义产物（CA-02）。
        config_hash = _organize_config_hash()
        input_hash = _organize_input_snapshot_hash(transcript_version_id)
        idem_key = compute_idempotency_key(
            subject_id=scope.subject_id,
            resource_id=lecture_id,
            job_type="organize",
            input_snapshot_hash=input_hash,
            semantic_config_hash=config_hash,
        )
        existing = (
            await db.execute(
                select(ProcessingJob).where(ProcessingJob.idempotency_key == idem_key)
            )
        ).scalars().first()
        if existing is not None:
            return existing

        job = ProcessingJob(
            type="organize",
            resource_id=lecture_id,
            input_version=transcript_version_id,
            config_hash=config_hash,
            subject_id=scope.subject_id,
            idempotency_key=idem_key,
            input_snapshot_ref=str(transcript_version_id),
            config_snapshot=_organize_config_snapshot(),
            policy_version=policy_version(),
            status="queued",
            stage="created",
            attempt=0,
            cancel_requested=False,
        )
        db.add(job)
        try:
            # flush 获取 job.id；并发创建时 INSERT 可能在此触发唯一约束冲突（CA-01）
            await db.flush()
            db.add(
                OutboxEvent(
                    business_key=f"job:{job.id}",
                    event_type="organize_requested",
                    job_id=job.id,
                    payload={"job_id": str(job.id), "resource_id": str(lecture_id)},
                    status="pending",
                    attempt=0,
                )
            )
            await db.commit()
        except IntegrityError:
            # 并发创建：唯一约束兜底，返回已有任务而非报错（CA-01）
            await db.rollback()
            existing = (
                await db.execute(
                    select(ProcessingJob).where(ProcessingJob.idempotency_key == idem_key)
                )
            ).scalars().first()
            return existing
        await db.refresh(job)
        return job

    @staticmethod
    def _manifest_payload(manifest: ChunkManifest) -> str:
        return norm_json({"input_segment_ids": manifest.input_segment_ids, "config": manifest.config})

    # ── 分块清单 ──────────────────────────────────────────────────────
    async def build_manifest(self, db: AsyncSession, job: ProcessingJob) -> ChunkManifest:
        existing = (
            await db.execute(
                select(ChunkManifest).where(ChunkManifest.job_id == job.id)
            )
        ).scalars().first()
        if existing is not None:
            cp = await get_checkpoint(db, job.id, "organize.manifest")
            if cp is not None and cp.status == "succeeded":
                # 复用成功 manifest 前核对 schema/输入/配置身份 + 清单摘要（CA-02）
                if not checkpoint_intact(
                    cp,
                    input_hash=_organize_input_snapshot_hash(job.input_version),
                    config_hash=job.config_hash,
                ):
                    raise CheckpointCorrupt()
                if cp.output_ref != str(existing.id):
                    raise CheckpointCorrupt()
                if cp.output_hash != sha256_hex(self._manifest_payload(existing)):
                    raise CheckpointCorrupt()
                return existing
            return existing

        segments = await _load_segments(db, job.input_version)
        # 冻结语义配置：恢复旧 job 时沿用创建时的分块参数，不悄然读当前全局 settings（CA-02）。
        snapshot = job.config_snapshot or _organize_config_snapshot()
        context_budget = int(snapshot.get("context_budget", settings.ORGANIZE_CONTEXT_BUDGET))
        prompt_reserve = int(snapshot.get("prompt_reserve", settings.ORGANIZE_PROMPT_RESERVE))
        output_reserve = int(snapshot.get("output_reserve", settings.ORGANIZE_OUTPUT_RESERVE))
        input_budget = context_budget - prompt_reserve - output_reserve
        overlap = int(snapshot.get("chunk_overlap", settings.ORGANIZE_CHUNK_OVERLAP))
        seq_len = {s["seq_index"]: len(s["content"] or "") for s in segments}

        def _core_len(core: list[int]) -> int:
            return sum(seq_len[i] for i in core)

        def _context_of(cores: list[list[int]], i: int) -> list[int]:
            ctx: list[int] = []
            if overlap > 0:
                if i > 0:
                    ctx += cores[i - 1][-overlap:]
                if i < len(cores) - 1:
                    ctx += cores[i + 1][:overlap]
            return [s for s in ctx if s not in cores[i]]

        # 贪心切块：核心片段按序分配，单片段原子不可拆。
        # 输入预算须覆盖「核心 + 相邻重叠上下文」：先按核心预算贪心，再依据实际
        # 上下文长度收缩核心预算，直到每块 core+context ≤ input_budget（P03-03）。
        core_budget = input_budget
        cores: list[list[int]] = []
        for _ in range(64):
            cores = []
            current: list[int] = []
            current_len = 0
            for seg in segments:
                seg_len = seq_len[seg["seq_index"]]
                if current and current_len + seg_len > core_budget:
                    cores.append(current)
                    current = [seg["seq_index"]]
                    current_len = seg_len
                else:
                    current.append(seg["seq_index"])
                    current_len += seg_len
            if current:
                cores.append(current)

            max_ctx = 0
            fits = True
            for i, core in enumerate(cores):
                ctx_len = sum(seq_len[s] for s in _context_of(cores, i))
                max_ctx = max(max_ctx, ctx_len)
                if _core_len(core) + ctx_len > input_budget:
                    fits = False
            if fits:
                break
            new_budget = input_budget - max_ctx
            if new_budget >= core_budget or new_budget <= 0:
                # 单片段自身已逼近/超过预算，无法继续收缩；保留当前分块。
                break
            core_budget = new_budget

        manifest = ChunkManifest(
            job_id=job.id,
            lecture_id=job.resource_id,
            transcript_version_id=job.input_version,
            input_segment_ids=[s["seq_index"] for s in segments],
            chunk_count=len(cores),
            config={
                "context_budget": context_budget,
                "prompt_reserve": prompt_reserve,
                "output_reserve": output_reserve,
                "overlap": overlap,
                "max_attempts": settings.ORGANIZE_MAX_ATTEMPTS,
                "input_budget": input_budget,
            },
        )
        db.add(manifest)
        await db.flush()

        for i, core in enumerate(cores):
            context: list[int] = []
            if i > 0:
                context += cores[i - 1][-overlap:]
            if i < len(cores) - 1:
                context += cores[i + 1][:overlap]
            context = [s for s in context if s not in core]
            db.add(
                ChunkItem(
                    manifest_id=manifest.id,
                    chunk_index=i,
                    core_segments=core,
                    context_segments=context,
                    status="pending",
                    attempt=0,
                )
            )
        await mark_checkpoint_succeeded(
            db,
            job_id=job.id,
            step_key="organize.manifest",
            input_hash=_organize_input_snapshot_hash(job.input_version),
            config_hash=job.config_hash,
            output_ref=str(manifest.id),
            output_hash=sha256_hex(self._manifest_payload(manifest)),
        )
        await db.commit()
        await db.refresh(manifest)
        return manifest

    # ── 主流程 ────────────────────────────────────────────────────────
    async def run_organize_job(
        self, db: AsyncSession, job_id: uuid.UUID, model=None
    ) -> ProcessingJob | None:
        job = await db.get(ProcessingJob, job_id)
        if job is None:
            return None
        if job.status in ("succeeded", "cancelled"):
            return job
        if job.cancel_requested:
            job.status = "cancelled"
            job.error_code = "cancelled"
            await db.commit()
            await db.refresh(job)
            return job

        # 真实整理模型须绑定 job 冻结语义配置，恢复旧 job 不读当前全局模型/分块参数（CA-02）。
        model = model or get_organize_model(job.config_snapshot)

        # 租约原子领取（CA-11）：同一时刻仅一个有效 owner，他人有效持有则本次不处理。
        owner = lease_service.new_owner()
        claimed, epoch = await lease_service.claim_job(db, job_id, owner)
        if not claimed:
            await db.commit()
            await db.refresh(job)
            return job
        await db.commit()
        await db.refresh(job)

        # 长调用期间独立 session 周期续租（CA-11）：外发 summarize/extract 期间租约不因
        # 主 session 阻塞而过期；失去租约后心跳自行停止。
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            lease_service.heartbeat_loop(job.id, owner, epoch, heartbeat_stop)
        )

        try:
            manifest = await self.build_manifest(db, job)
            await _check_cancel(db, job)

            # 规划（A3 规划决定，本地编排、非外部调用）：按输入与可用预算决定四类分支
            # 选中/跳过。网关路径把当前主体日可用预算传给生产规划器，供其按预算裁剪。
            plan_meta = {"title": await self._lecture_title(db, job.resource_id)}
            if settings.CALL_GATEWAY_ENABLED:
                from app.services import call_budget_service as budget_svc

                avail = await budget_svc.available_budget(db, job.subject_id)
                plan_meta["available_budget_micro"] = avail["subject_daily"]["available_micro"]
                plan_meta["branch_cost_micro"] = budget_svc.reserve_min_micro()
            plan = await model.plan(
                await _load_segments(db, job.input_version),
                plan_meta,
            )
            # 规划落库（A6 运行详情：预算阻止/超时等无笔记终态也能按作业 ID 查四类分支选择）。
            await mark_checkpoint_succeeded(
                db,
                job_id=job.id,
                step_key="organize.plan",
                input_hash=_organize_input_snapshot_hash(job.input_version),
                config_hash=job.config_hash,
                output_data=plan,
                output_hash=sha256_hex(norm_json(plan)),
            )
            await db.commit()

            result = await self._process_chunks(
                db, job, manifest, model, owner=owner, epoch=epoch, plan=plan
            )
            conclusions = result["conclusions"]
            knowledge_points = result["knowledge_points"]
            examples = result["examples"]
            key_points = result["key_points"]
            complete = result["complete"]
            succeeded = result["succeeded"]
            total = result["total"]
            executed_branches = result["executed_branches"]
            await _check_cancel(db, job)

            if not complete:
                # 部分成功：保留草稿但不发布，明确终态（P03-12）。
                # 提交前校验租约仍归本执行者（CA-11）：迟到部分失败不得改写新 owner 状态。
                if not await lease_service.assert_owner(db, job_id, owner, epoch, for_update=True):
                    raise lease_service.StaleLeaseError()
                note = await self._upsert_note(
                    db, job, conclusions, knowledge_points, examples, key_points,
                    complete=False, succeeded=succeeded, total=total, plan=plan,
                    executed_branches=executed_branches,
                )
                job.output_version = note.id
                job.status = "failed"
                job.stage = "coverage_incomplete"
                job.error_type = "incomplete_coverage"
                job.error_code = "incomplete_coverage"
                await db.commit()
                await db.refresh(job)
                return job

            # ── 阶段 A：最终汇总（CA-04）——结果与 Checkpoint 一并落库，恢复复用不重复 summarize ──
            await _check_cancel(db, job)
            summary_cp = await get_checkpoint(db, job.id, "organize.summary")
            if summary_cp is not None and summary_cp.status == "succeeded":
                # 复用前核对摘要/schema/输入/配置身份（CA-02）：被篡改或身份不符 → checkpoint_corrupt，
                # 不发布损坏产物、不静默重建。
                if not checkpoint_intact(
                    summary_cp,
                    input_hash=_organize_input_snapshot_hash(job.input_version),
                    config_hash=job.config_hash,
                ):
                    raise CheckpointCorrupt()
                # 复用汇总结果：跳过 summarize 外发，记录复用次数（CA-24）
                await mark_checkpoint_reused(db, job.id)
                stored = summary_cp.output_data or {}
                title = stored.get("title") or await self._lecture_title(db, job.resource_id)
                summary = stored.get("summary")
                conclusions = stored.get("conclusions") or conclusions
                knowledge_points = stored.get("knowledge_points") or knowledge_points
                examples = stored.get("examples") or examples
                key_points = stored.get("key_points") or key_points
            else:
                # 汇总为外发调用：过期 epoch 不得发起新调用（CA-11）
                if not await lease_service.assert_owner(db, job_id, owner, epoch):
                    raise lease_service.StaleLeaseError()
                title = await self._lecture_title(db, job.resource_id)
                if settings.CALL_GATEWAY_ENABLED:
                    # 网关路径（S3）：summarize 与 extract 共用生成共享桶并经统一网关计费。
                    from app.services.ai_call_gateway import CallSpec, call_controlled

                    spec = CallSpec(
                        operation_key=f"organize:summarize:{job.id}",
                        operation_type="organize_summarize",
                        operation_family=rate_limit_service.FAMILY_GEN,
                        provider=model.name,
                        model=getattr(model, "model", None),
                        job_id=job.id,
                        checkpoint_id=job.id,
                        subject_id=job.subject_id,
                        is_idempotent=False,
                        owner=owner,
                        epoch=epoch,
                        result_serializer=json_serializer,
                        result_deserializer=json_deserializer,
                    )
                    outcome = await call_controlled(
                        db, spec,
                        lambda: model.summarize(
                            conclusions, knowledge_points, examples, key_points, {"title": title}
                        ),
                    )
                    if outcome.kind == "success":
                        summary = outcome.result
                    elif outcome.kind == "unknown":
                        raise ProviderUnknownError("汇总结果未知，阻断普通重试")
                    elif outcome.error is not None:
                        raise outcome.error
                    else:
                        raise ModelError("provider_error", outcome.error_code or "summarize_failed",
                                         retryable=False)
                else:
                    # 供应商生成/整理共享桶（CA-16）：extract 与 summarize 共用同一共享配额组。
                    await rate_limit_service.consume_provider_bucket(
                        rate_limit_service.FAMILY_GEN, model.name
                    )
                    summary = await model.summarize(
                        conclusions, knowledge_points, examples, key_points, {"title": title}
                    )
                # 外发响应返回后、提交汇总 Checkpoint 前：加行锁重新核对归属（CA-11），
                # 迟到响应不得作为旧执行者成功产物提交。
                if not await lease_service.assert_owner(db, job_id, owner, epoch, for_update=True):
                    raise lease_service.StaleLeaseError()
                summary_payload = {
                    "title": title,
                    "summary": summary,
                    "conclusions": conclusions,
                    "knowledge_points": knowledge_points,
                    "examples": examples,
                    "key_points": key_points,
                }
                await mark_checkpoint_succeeded(
                    db,
                    job_id=job.id,
                    step_key="organize.summary",
                    input_hash=_organize_input_snapshot_hash(job.input_version),
                    config_hash=job.config_hash,
                    output_data=summary_payload,
                    output_hash=sha256_hex(norm_json(summary_payload)),
                )
                # 阶段 A 独立落库：此后崩溃仅恢复发布阶段，不重复 summarize（CA-04）
                await db.commit()
                if _post_summary_hook is not None:
                    _post_summary_hook()

            content = self._validate_and_assemble(
                summary, conclusions, knowledge_points, examples, key_points, job.input_version,
                complete=True, succeeded=succeeded, total=total, plan=plan,
                executed_branches=executed_branches,
            )

            # ── 阶段 B：建可审阅草稿 + note.draft Checkpoint，原子提交 ──
            # A4：完整整理先生成可审阅草稿，不自动发布。首次显式确认前不切换当前发布指针、
            # 不创建正式索引意向、不把草稿加入正式来源；发布由显式 publish_note 完成。
            await _check_cancel(db, job)
            # 提交草稿前校验租约仍归本执行者（CA-11）：加行锁使「校验归属 → 写产物 → 提交」
            # 在同一事务内原子，过期 epoch 不得提交（不先查 bool 再无条件提交）。
            if not await lease_service.assert_owner(db, job_id, owner, epoch, for_update=True):
                raise lease_service.StaleLeaseError()

            note = await self._upsert_note(
                db, job, conclusions, knowledge_points, examples, key_points,
                content=content, complete=True, succeeded=succeeded, total=total,
                executed_branches=executed_branches,
            )
            job.output_version = note.id

            await mark_checkpoint_succeeded(
                db,
                job_id=job.id,
                step_key="note.draft",
                input_hash=_organize_input_snapshot_hash(job.input_version),
                config_hash=job.config_hash,
                output_ref=str(note.id),
                output_hash=sha256_hex(norm_json(note.content)),
            )

            job.status = "succeeded"
            job.stage = "draft"
            job.last_successful_stage = "note.draft"
            job.error_code = None
            job.error_type = None
            await db.commit()
            await db.refresh(job)
            return job
        except OrganizeCancelled:
            # 执行者响应取消也受租约保护（CA-11）：迟到取消不得改写新 owner 状态
            def _cancel(row: ProcessingJob) -> None:
                row.status = "cancelled"
                row.error_code = "cancelled"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _cancel):
                return await self._reload_job(db, job_id)
            await db.refresh(job)
            return job
        except CallCancelled:
            # 网关在再次外发前检测到取消（CA-23）：与 OrganizeCancelled 同样标记 cancelled。
            def _cancel(row: ProcessingJob) -> None:
                row.status = "cancelled"
                row.error_code = "cancelled"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _cancel):
                return await self._reload_job(db, job_id)
            await db.refresh(job)
            return job
        except lease_service.StaleLeaseError:
            # 租约已被接管/过期：不提交产物、不改写任务状态，交由新 owner 处理（CA-11）
            return await self._reload_job(db, job_id)
        except WorkerCrash:
            # 模拟 worker 失联：不改写任务终态，已提交的块产物由恢复沿用
            raise
        except rate_limit_service.RateLimitedError as e:
            # 供应商桶拒绝（CA-16）：落库 retry_wait 并释放租约，不消耗外部 attempt_no、不出网。
            if await rate_limit_service.mark_control_wait(
                db, job_id, owner, epoch, "rate_limited", e.retry_after_seconds
            ):
                return await self._reload_job(db, job_id)
            return await self._reload_job(db, job_id)
        except rate_limit_service.RateLimitUnavailableError:
            # Redis 不可用（CA-16）：worker 持久等待 ≥5s 再检查，禁止本地无限放行、禁止出网。
            if await rate_limit_service.mark_control_wait(
                db, job_id, owner, epoch, "rate_limit_unavailable", settings.CHECKPOINT_REDIS_WAIT_SECONDS
            ):
                return await self._reload_job(db, job_id)
            return await self._reload_job(db, job_id)
        except ControlWaitError as e:
            # 供应商在途 permit 已满（CA-17）：落库控制等待，不占 permit/预算/外部 attempt。
            if await rate_limit_service.mark_control_wait(
                db, job_id, owner, epoch, "concurrency_limit",
                e.wait_seconds or settings.CHECKPOINT_PROVIDER_PERMIT_TTL_SECONDS
            ):
                return await self._reload_job(db, job_id)
            return await self._reload_job(db, job_id)
        except BudgetExhaustedError:
            # 持久预算不足（CA-18/CA-19）：保留 Checkpoint，明确失败不重试、不出网。
            def _budget(row: ProcessingJob) -> None:
                row.error_type = "budget_exhausted"
                row.error_code = "budget_exhausted"
                row.status = "failed"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _budget):
                return await self._reload_job(db, job_id)
            await db.refresh(job)
            return job
        except ProviderUnknownError:
            # 非幂等外部结果未知（CA-07）：进入 provider_unknown，阻断普通重试，保留未知预留。
            def _unknown_call(row: ProcessingJob) -> None:
                row.error_type = "provider_unknown"
                row.error_code = "provider_unknown"
                row.status = "failed"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _unknown_call):
                return await self._reload_job(db, job_id)
            await db.refresh(job)
            return job
        except CheckpointCorrupt:
            # 已成功 Checkpoint 引用/摘要/schema 失效：明确失败并保留拒绝原因（CA-02），
            # 不静默重建、不伪成功、不发起新外部调用。
            def _corrupt(row: ProcessingJob) -> None:
                row.error_type = "checkpoint_corrupt"
                row.error_code = "checkpoint_corrupt"
                row.status = "failed"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _corrupt):
                return await self._reload_job(db, job_id)
            await db.refresh(job)
            return job
        except ModelError as e:
            if not await self._mark_failure(db, job, e, owner, epoch):
                return await self._reload_job(db, job_id)
            await db.refresh(job)
            return job
        except ServiceError as e:
            def _svc(row: ProcessingJob) -> None:
                row.error_type = e.code
                row.error_code = e.code
                row.status = "failed"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _svc):
                return await self._reload_job(db, job_id)
            await db.refresh(job)
            return job
        except Exception as e:  # noqa: BLE001
            logger.exception("organize job %s 处理异常", job.id)
            # 回滚未提交的笔记/发布/证据，避免留下半发布结果（P03-11 原子发布）；
            # 再以受租约保护的短事务写终态（CA-11），迟到异常不得改写新 owner 状态。
            def _unknown(row: ProcessingJob) -> None:
                row.error_type = "provider_error"
                row.error_code = "unknown"
                row.status = "failed"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _unknown):
                return await self._reload_job(db, job_id)
            await db.refresh(job)
            return job
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _process_chunks(
        self, db: AsyncSession, job: ProcessingJob, manifest: ChunkManifest, model,
        owner: str | None = None, epoch: int = 0, plan: dict | None = None,
    ) -> dict:
        items = (
            await db.execute(
                select(ChunkItem)
                .where(ChunkItem.manifest_id == manifest.id)
                .order_by(ChunkItem.chunk_index)
            )
        ).scalars().all()
        segments = {s["seq_index"]: s for s in await _load_segments(db, job.input_version)}

        conclusions: list[dict] = []
        knowledge_points: list[dict] = []
        examples: list[dict] = []
        key_points: list[dict] = []
        succeeded = 0
        total = len(items)

        # A3 调度：按规划只产出选中分支；执行状态取自真实运行事件（是否实际外发过 extract）。
        selected_branches = _selected_branch_names(plan)
        extract_invoked = False
        reused = False

        def _extend_branch_outputs(out: dict) -> None:
            # 仅选中分支接纳产物（未选中分支被跳过，即使模型返回也不产出）。
            if "note" in selected_branches:
                conclusions.extend(out.get("conclusions", []))
            if "knowledge_point" in selected_branches:
                knowledge_points.extend(out.get("knowledge_points", []))
            if "example" in selected_branches:
                examples.extend(out.get("examples", []))
            if "key_point" in selected_branches:
                key_points.extend(out.get("key_points", []))

        for item in items:
            await _check_cancel(db, job)
            if item.status == "succeeded" and item.output is not None:
                cp = await get_checkpoint(db, job.id, "organize.extract", str(item.chunk_index))
                if cp is not None and cp.status == "succeeded":
                    # 复用成功块前核对 schema/输入/配置身份 + 输出引用/摘要（CA-02）
                    if not checkpoint_intact(
                        cp,
                        input_hash=_organize_input_snapshot_hash(job.input_version),
                        config_hash=job.config_hash,
                    ):
                        raise CheckpointCorrupt()
                    if cp.output_ref != str(item.id):
                        raise CheckpointCorrupt()
                    if cp.output_hash != sha256_hex(norm_json(item.output)):
                        raise CheckpointCorrupt()
                    # 复用只补缺失：命中成功块即跳过外部调用，记录复用次数（CA-24）
                    await mark_checkpoint_reused(db, job.id)
                out = item.output
                _extend_branch_outputs(out)
                reused = True
                succeeded += 1
                continue

            chunk = ChunkInput(
                chunk_index=item.chunk_index,
                core=[segments[i] for i in (item.core_segments or []) if i in segments],
                context=[segments[i] for i in (item.context_segments or []) if i in segments],
                selected_branches=frozenset(selected_branches),
            )
            item.status = "running"
            item.attempt = (item.attempt or 0) + 1
            item.started_at = _now()
            await db.commit()

            out = None
            if settings.CALL_GATEWAY_ENABLED:
                # 网关路径（S3）：extract 经统一网关受控计费；有界退避在 call_controlled 内，
                # 块内不再自身 while 重试（CA-21 无多层放大）。
                from app.services.ai_call_gateway import CallSpec, call_controlled

                try:
                    if owner and not await lease_service.assert_owner(db, job.id, owner, epoch):
                        raise lease_service.StaleLeaseError()
                    spec = CallSpec(
                        operation_key=f"organize:extract:{job.id}:{item.chunk_index}",
                        operation_type="organize_extract",
                        operation_family=rate_limit_service.FAMILY_GEN,
                        provider=model.name,
                        model=getattr(model, "model", None),
                        job_id=job.id,
                        checkpoint_id=job.id,
                        subject_id=job.subject_id,
                        is_idempotent=False,
                        owner=owner,
                        epoch=epoch,
                        result_serializer=json_serializer,
                        result_deserializer=json_deserializer,
                    )
                    extract_invoked = True
                    outcome = await call_controlled(db, spec, lambda: model.extract_chunk(chunk))
                    if outcome.kind == "success":
                        out = outcome.result
                        if not self._validate_chunk_output(out):
                            item.status = "failed"
                            item.error_code = "invalid_result"
                            item.finished_at = _now()
                            await db.commit()
                            break
                        if owner and not await lease_service.assert_owner(db, job.id, owner, epoch, for_update=True):
                            raise lease_service.StaleLeaseError()
                        item.output = out
                        item.status = "succeeded"
                        item.error_code = None
                        item.finished_at = _now()
                        await mark_checkpoint_succeeded(
                            db,
                            job_id=job.id,
                            step_key="organize.extract",
                            unit_key=str(item.chunk_index),
                            input_hash=_organize_input_snapshot_hash(job.input_version),
                            config_hash=job.config_hash,
                            output_ref=str(item.id),
                            output_hash=sha256_hex(norm_json(out)),
                        )
                        await db.commit()
                    else:
                        item.status = "failed"
                        item.error_code = (
                            "provider_unknown" if outcome.kind == "unknown"
                            else (outcome.error_code or "extract_failed")
                        )
                        item.finished_at = _now()
                        await db.commit()
                except lease_service.StaleLeaseError:
                    raise
                except OrganizeCancelled:
                    raise
                except CallCancelled:
                    # 网关检测到取消（CA-23）：向上抛，由 run_organize_job 标记 cancelled。
                    raise
                except WorkerCrash:
                    raise
                except (rate_limit_service.RateLimitedError, rate_limit_service.RateLimitUnavailableError):
                    raise
                except (ControlWaitError, BudgetExhaustedError):
                    raise
            else:
                while True:
                    try:
                        if owner and not await lease_service.assert_owner(db, job.id, owner, epoch):
                            raise lease_service.StaleLeaseError()
                        # 供应商生成/整理共享桶（CA-16）：拒绝/Redis 不可用向上抛，不立即重试。
                        await rate_limit_service.consume_provider_bucket(
                            rate_limit_service.FAMILY_GEN, model.name
                        )
                        extract_invoked = True
                        out = await model.extract_chunk(chunk)
                        if not self._validate_chunk_output(out):
                            raise ModelError("invalid_result", "块输出缺必填字段或类型错误", retryable=False)
                        # 外发响应返回后、提交块产物前：加行锁重新核对归属（CA-11），
                        # 迟到响应不得作为旧执行者成功产物提交；失败即 rollback 停止。
                        if owner and not await lease_service.assert_owner(db, job.id, owner, epoch, for_update=True):
                            raise lease_service.StaleLeaseError()
                        item.output = out
                        item.status = "succeeded"
                        item.error_code = None
                        item.finished_at = _now()
                        await mark_checkpoint_succeeded(
                            db,
                            job_id=job.id,
                            step_key="organize.extract",
                            unit_key=str(item.chunk_index),
                            input_hash=_organize_input_snapshot_hash(job.input_version),
                            config_hash=job.config_hash,
                            output_ref=str(item.id),
                            output_hash=sha256_hex(norm_json(out)),
                        )
                        await db.commit()
                        break
                    except ModelError as e:
                        if e.retryable and (item.attempt or 0) < settings.ORGANIZE_MAX_ATTEMPTS:
                            item.attempt = (item.attempt or 0) + 1
                            await db.commit()
                            continue
                        item.status = "failed"
                        item.error_code = e.code
                        item.finished_at = _now()
                        await db.commit()
                        break
                    except lease_service.StaleLeaseError:
                        # 失去租约不得被通用重试分支吞掉后改写新 owner 状态（CA-11）：向上抛出
                        raise
                    except OrganizeCancelled:
                        raise
                    except WorkerCrash:
                        raise
                    except (rate_limit_service.RateLimitedError, rate_limit_service.RateLimitUnavailableError):
                        # 控制等待不得进入块内立即重试循环（CA-16）：向上抛，交由 run_organize_job 落库 retry_wait。
                        raise
                    except Exception as e:  # noqa: BLE001
                        logger.exception("organize chunk %s 异常", item.chunk_index)
                        if (item.attempt or 0) < settings.ORGANIZE_MAX_ATTEMPTS:
                            item.attempt = (item.attempt or 0) + 1
                            await db.commit()
                            continue
                        item.status = "failed"
                        item.error_code = "unknown"
                        item.finished_at = _now()
                        await db.commit()
                        break

            if item.status == "succeeded":
                _extend_branch_outputs(out)
                succeeded += 1

        # 去重：同文条目只保留一条（同源重复不重复形成正式条目）
        def _dedup(items: list[dict], key_field: str) -> list[dict]:
            seen: set[str] = set()
            out: list[dict] = []
            for it in items:
                key = (it.get(key_field) or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(it)
            return out

        deduped = _dedup(conclusions, "text")
        deduped_kp = _dedup(knowledge_points, "definition")
        deduped_ex = _dedup(examples, "problem")
        deduped_keypts = _dedup(key_points, "point")

        # 执行状态取自真实运行事件：选中且本任务实际外发过 extract（或复用已成功块）的分支
        # 才计为「已执行」；未选中分支「跳过」不执行。预算阻止（不出网）时无分支被执行。
        executed_branches = selected_branches if (extract_invoked or reused) else set()

        return {
            "conclusions": deduped,
            "knowledge_points": deduped_kp,
            "examples": deduped_ex,
            "key_points": deduped_keypts,
            "complete": succeeded == total,
            "succeeded": succeeded,
            "total": total,
            "executed_branches": executed_branches,
        }

    async def _lecture_title(self, db: AsyncSession, lecture_id: uuid.UUID) -> str:
        meeting = await db.get(Meeting, lecture_id)
        return (meeting.title if meeting else "课堂笔记") or "课堂笔记"

    def _validate_and_assemble(
        self, summary, conclusions, knowledge_points, examples, key_points,
        transcript_version_id, *, complete, succeeded, total, plan=None,
        executed_branches=None,
    ) -> dict:
        # 结构校验：缺必填字段 / 类型错误 → 明确失败，不伪成功（P03-07）
        if not isinstance(summary, dict):
            raise ServiceError(422, ERR_INVALID_RESULT, "汇总结果不是对象", retryable=False)
        title = summary.get("title")
        outline = summary.get("outline")
        if not isinstance(title, str) or not title.strip():
            raise ServiceError(422, ERR_INVALID_RESULT, "笔记缺少 title", retryable=False)
        if not isinstance(outline, list):
            raise ServiceError(422, ERR_INVALID_RESULT, "笔记缺少 outline", retryable=False)

        key_conclusions = [self._conclusion_point(c) for c in conclusions]
        knowledge_point_entries = [self._knowledge_point(k) for k in knowledge_points]
        example_entries = [self._example_point(e) for e in examples]
        key_point_entries = [self._key_point(p) for p in key_points]
        sections = [{"heading": "要点", "points": key_conclusions}]

        # 分支选中/跳过/执行（A3）：选中取自规划决定（无规划则按产物是否非空判定）。
        # 「执行」取自真实运行事件（executed_branches）：只有选中且实际外发过 extract 的
        # 分支才算「已执行」；未选中分支「跳过」不执行（skipped=true、executed=false），
        # 区别于「选中但产物为空」（executed=true、count=0）。
        plan_branches = (plan or {}).get("branches") or {}

        def _branch_plan(name: str, count: int) -> dict:
            selected = plan_branches.get(name, {}).get("selected", count > 0)
            if executed_branches is None:
                executed = bool(selected) and count >= 0
            else:
                executed = name in executed_branches
            return {
                "selected": bool(selected),
                "skipped": not bool(selected),
                "executed": bool(executed),
                "count": count,
            }

        return {
            "title": title,
            "outline": outline,
            "sections": sections,
            "key_conclusions": key_conclusions,
            "knowledge_points": knowledge_point_entries,
            "examples": example_entries,
            "key_points": key_point_entries,
            "plan": plan,
            "branches": {
                "note": _branch_plan("note", len(key_conclusions)),
                "knowledge_point": _branch_plan("knowledge_point", len(knowledge_point_entries)),
                "example": _branch_plan("example", len(example_entries)),
                "key_point": _branch_plan("key_point", len(key_point_entries)),
            },
            "source_version": {"transcript_version_id": str(transcript_version_id)},
            "coverage": {
                "complete": complete,
                "total_chunks": total,
                "succeeded_chunks": succeeded,
            },
        }

    @staticmethod
    def _validate_chunk_output(out) -> bool:
        return (
            isinstance(out, dict)
            and isinstance(out.get("conclusions"), list)
            and isinstance(out.get("knowledge_points"), list)
            and isinstance(out.get("examples"), list)
            and isinstance(out.get("key_points"), list)
        )

    @staticmethod
    def _conclusion_point(c: dict) -> dict:
        entry = {
            "text": c.get("text"),
            "quote": c.get("quote"),
            "origin": c.get("origin", "teacher_explicit"),
            "status": c.get("status", "confirmed"),
            "evidence_ids": [],
        }
        entry["_verified_body"] = _entry_body_signature(entry, "key_conclusions")
        return entry

    @staticmethod
    def _knowledge_point(k: dict) -> dict:
        entry = {
            "definition": k.get("definition"),
            "applicability": k.get("applicability"),
            "quote": k.get("quote"),
            "origin": k.get("origin", "material_derived"),
            "status": k.get("status", "confirmed"),
            "evidence_ids": [],
        }
        entry["_verified_body"] = _entry_body_signature(entry, "knowledge_points")
        return entry

    @staticmethod
    def _example_point(e: dict) -> dict:
        entry = {
            "problem": e.get("problem"),
            "derivation": e.get("derivation"),
            "quote": e.get("quote"),
            "origin": e.get("origin", "material_derived"),
            "status": e.get("status", "confirmed"),
            "evidence_ids": [],
        }
        entry["_verified_body"] = _entry_body_signature(entry, "examples")
        return entry

    @staticmethod
    def _key_point(p: dict) -> dict:
        entry = {
            "point": p.get("point"),
            "reason": p.get("reason"),
            "quote": p.get("quote"),
            "origin": p.get("origin", "material_derived"),
            "status": p.get("status", "confirmed"),
            "evidence_ids": [],
        }
        entry["_verified_body"] = _entry_body_signature(entry, "key_points")
        return entry

    async def _upsert_note(
        self, db: AsyncSession, job: ProcessingJob,
        conclusions, knowledge_points, examples, key_points,
        *, complete, succeeded, total, content=None, plan=None,
        executed_branches=None,
    ) -> NoteVersion:
        note_id = job.output_version
        note = await db.get(NoteVersion, note_id) if note_id else None
        if note is None:
            note = NoteVersion(
                lecture_id=job.resource_id,
                transcript_version_id=job.input_version,
                pipeline_version=job.config_hash,
                status="draft",
                content=None,
            )
            db.add(note)
            await db.flush()
        else:
            # 重建证据：删除旧证据关联与证据（本笔记专属），再重建，保证重试幂等
            await self._clear_note_evidence(db, note)

        if content is None:
            content = self._validate_and_assemble(
                {"title": await self._lecture_title(db, job.resource_id), "outline": ["要点"]},
                conclusions, knowledge_points, examples, key_points, job.input_version,
                complete=complete, succeeded=succeeded, total=total, plan=plan,
                executed_branches=executed_branches,
            )

        # 解析证据并注入 evidence_ids（时间以可信来源为准，模型不可伪造）
        resolved, evidence_ids = await self._resolve_evidence(
            db, note, job.input_version,
            conclusions, knowledge_points, examples, key_points,
        )
        # 无可验证引用的条目不得作为「已核对」发布（P03-09/P03-10）：降待确认。
        for point in content.get("key_conclusions", []):
            key = (point.get("text") or "").strip()
            ids = resolved.get(key, [])
            point["evidence_ids"] = ids
            if not ids and point.get("status") == "confirmed":
                point["status"] = "pending"
        for kp in content.get("knowledge_points", []):
            key = (kp.get("definition") or "").strip()
            ids = resolved.get(key, [])
            kp["evidence_ids"] = ids
            if not ids and kp.get("status") == "confirmed":
                kp["status"] = "pending"
        for ex in content.get("examples", []):
            key = (ex.get("problem") or "").strip()
            ids = resolved.get(key, [])
            ex["evidence_ids"] = ids
            if not ids and ex.get("status") == "confirmed":
                ex["status"] = "pending"
        for pt in content.get("key_points", []):
            key = (pt.get("point") or "").strip()
            ids = resolved.get(key, [])
            pt["evidence_ids"] = ids
            if not ids and pt.get("status") == "confirmed":
                pt["status"] = "pending"

        note.content = content
        await db.flush()
        return note

    async def _clear_note_evidence(self, db: AsyncSession, note: NoteVersion) -> None:
        links = (
            await db.execute(
                select(ArtifactEvidence).where(
                    ArtifactEvidence.artifact_type == "note_version",
                    ArtifactEvidence.artifact_id == note.id,
                )
            )
        ).scalars().all()
        evidence_ids = [l.evidence_id for l in links]
        for link in links:
            await db.delete(link)
        if evidence_ids:
            for eid in evidence_ids:
                span = await db.get(EvidenceSpan, eid)
                if span is not None:
                    await db.delete(span)
        await db.flush()

    async def _resolve_evidence(
        self, db: AsyncSession, note: NoteVersion, version_id: uuid.UUID,
        conclusions, knowledge_points, examples, key_points,
    ) -> tuple[dict, list]:
        segments = {s["seq_index"]: s for s in await _load_segments(db, version_id)}
        resolved: dict[tuple, list] = {}
        evidence_ids: list = []
        # 四类条目：text/definition/problem/point 为来源锚点键，逐条解析证据。
        for item, key_field in (
            (conclusions, "text"),
            (knowledge_points, "definition"),
            (examples, "problem"),
            (key_points, "point"),
        ):
            for it in item:
                key = (it.get(key_field) or "").strip()
                span = await self._create_span(db, note, version_id, segments, it)
                if span is not None:
                    resolved.setdefault(key, []).append(str(span.id))
                    evidence_ids.append(str(span.id))
        return resolved, evidence_ids

    @staticmethod
    def _product_text(item: dict) -> str:
        for k in ("text", "definition", "problem", "point", "task"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    async def _create_span(
        self, db: AsyncSession, note: NoteVersion, version_id: uuid.UUID,
        segments: dict, c: dict,
    ) -> EvidenceSpan | None:
        text = self._product_text(c)
        # 模型不得指定 evidence_id / 回听时间；出现即视为待确认（不发布为已验证结论）
        if c.get("evidence_id") is not None or c.get("start_ms") is not None or c.get("end_ms") is not None:
            return None
        seq = c.get("segment_index")
        if seq is None or seq not in segments:
            return None
        seg = segments[seq]
        quote = (c.get("quote") or text or "").strip()
        if not quote or quote not in (seg["content"] or ""):
            return None

        span = EvidenceSpan(
            transcript_version_id=version_id,
            segment_start=seq,
            segment_end=seq,
            start_ms=seg["start_ms"],  # 时间取自可信片段，缺失则 NULL，不伪造
            end_ms=seg["end_ms"],
            quote=quote,
            is_valid=True,
        )
        db.add(span)
        await db.flush()
        db.add(
            ArtifactEvidence(
                artifact_type="note_version",
                artifact_id=note.id,
                evidence_id=span.id,
            )
        )
        await db.flush()
        return span

    async def _reverify_note_evidence(self, db: AsyncSession, note: NoteVersion) -> None:
        """发布前重新核对编辑后的正文与来源（A4 返工 v2/v3）。

        逐条核对两类实质变化，失配/伪造条目的降「待确认（pending）」并清空 evidence_ids，
        从而从正式知识中排除（索引/检索仅取 confirmed）：
        1. 伪造证据字段（evidence_id / 回听时间）；
        2. 正文被改动（与创建时固化的 ``_verified_body`` 签名不一致）——旧引文仍存在不能
           单独作为新正文继续 confirmed 的依据，已变更且未重新核实的条目回到 pending；
        通过者优先以原证据定位（服务端可信、属于当前转写版本）重建证据关联；原定位缺失时以
        ``quote`` 逐字出现在来源片段中为准，同一引文命中多处（歧义）无法确定归属则留待核实。
        创建有效证据前核对当前引文确实存在于所属片段，失配则恢复可核实原引用或降待确认。
        """
        if note.transcript_version_id is None:
            return
        segments = {s["seq_index"]: s for s in await _load_segments(db, note.transcript_version_id)}
        content = note.content or {}
        # A4 返工 v8：重建证据前保留原证据定位（服务端可信、属于当前转写版本）。原证据 span 的
        # segment_start / start_ms / end_ms 是创建时按 chunk 的 segment_index 定的，比发布时按
        # quote 全表扫描更可信（同一引文在多片段出现时不会错定位到预告/其他片段）；先抓取原定位，
        # 再清除并重建。
        original_span_loc: dict[str, dict] = {}
        for list_key in ("key_conclusions", "knowledge_points", "examples", "key_points"):
            for entry in (content.get(list_key) or []):
                if not isinstance(entry, dict):
                    continue
                for eid in (entry.get("evidence_ids") or []):
                    key = str(eid)
                    if key in original_span_loc:
                        continue
                    try:
                        uid = uuid.UUID(key)
                    except (ValueError, TypeError):
                        continue
                    span = await db.get(EvidenceSpan, uid)
                    if span is not None and span.transcript_version_id == note.transcript_version_id:
                        original_span_loc[key] = {
                            "segment_start": span.segment_start,
                            "segment_end": span.segment_end,
                            "start_ms": span.start_ms,
                            "end_ms": span.end_ms,
                            "quote": span.quote,
                        }
        await self._clear_note_evidence(db, note)
        # A4 返工 v4：跨编辑稳定身份——旧证据 id → key_conclusion 条目引用。用于正文变化
        # （text 改变）时把 section 的旧 point 归属映射到重新核对后的结论，避免章节残留旧文。
        old_evidence_to_kc: dict[str, list[dict]] = {}
        for entry in (content.get("key_conclusions") or []):
            if isinstance(entry, dict):
                for eid in (entry.get("evidence_ids") or []):
                    old_evidence_to_kc.setdefault(str(eid), []).append(entry)
        for list_key, key_field in (
            ("key_conclusions", "text"),
            ("knowledge_points", "definition"),
            ("examples", "problem"),
            ("key_points", "point"),
        ):
            for entry in content.get(list_key) or []:
                if not isinstance(entry, dict):
                    continue
                # 模型/编辑不得伪造证据字段（evidence_id / 回听时间）
                if (entry.get("evidence_id") is not None
                        or entry.get("start_ms") is not None
                        or entry.get("end_ms") is not None):
                    entry["status"] = "pending"
                    entry["evidence_ids"] = []
                    continue
                # A4 v6：正文实质变化检测。有服务端基线（_verified_body）则比较——正文已变更 →
                # 旧引文不能单独支持新正文 → 降 pending。无基线（升级前旧草稿）禁止用「本次待
                # 发布正文」为自身建立已验证基线（循环自证），改以服务端可信记录 quote 为依据：
                # 锚点字段（正文主键）仍与 quote 一致 → 原样可核实，保留 confirmed；已偏离 quote
                # → 正文被改且无法核实 → 留 pending。客户端回传的内部签名已在 edit_note 剥离，不作依据。
                snap = entry.get("_verified_body")
                is_legacy_draft = snap is None
                if snap is not None:
                    if _entry_body_signature(entry, list_key) != snap:
                        entry["status"] = "pending"
                        entry["evidence_ids"] = []
                        continue
                else:
                    quote_raw = (entry.get("quote") or "").strip()
                    anchor_val = (entry.get(key_field) or "").strip()
                    if quote_raw and anchor_val and quote_raw != anchor_val:
                        entry["status"] = "pending"
                        entry["evidence_ids"] = []
                        continue
                quote = (entry.get("quote") or entry.get(key_field) or "").strip()
                # A4 返工 v8：优先使用原证据定位（服务端可信、属于当前转写版本）作为来源片段；原定位
                # 缺失时才按 quote 回退匹配，且同一引文命中多处（歧义）或零命中均无法确定归属 → 留待
                # 核实（pending）。不跨片段用其他不相关片段中的相同文字作依据。
                matched = None
                for eid in (entry.get("evidence_ids") or []):
                    loc = original_span_loc.get(str(eid))
                    if loc is not None and loc.get("segment_start") in segments:
                        matched = (loc["segment_start"], segments[loc["segment_start"]])
                        break
                if matched is None and quote:
                    hits = [seg for seg in segments.values() if quote in (seg["content"] or "")]
                    if len(hits) == 1:
                        matched = (hits[0]["seq_index"], hits[0])
                if matched is None:
                    entry["status"] = "pending"
                    entry["evidence_ids"] = []
                    continue
                seq, seg = matched
                seg_text = seg["content"] or ""
                # A4 v9：创建有效证据前，核对当前引文确实存在于该条目所属片段（不跨片段用其他片段
                # 文字背书）。失配（引文被改或不属于该片段）→ 拒绝该引文：优先恢复原证据定位里可核实
                # 的原引用（在该片段内逐字命中）；无法恢复则降 pending 并清空有效证据。
                if quote and quote not in seg_text:
                    restored = None
                    for eid in (entry.get("evidence_ids") or []):
                        loc = original_span_loc.get(str(eid))
                        if loc is not None and loc.get("segment_start") == seq:
                            oq = (loc.get("quote") or "").strip()
                            if oq and oq in seg_text:
                                restored = oq
                                break
                    if restored:
                        quote = restored
                        entry["quote"] = restored
                    else:
                        entry["status"] = "pending"
                        entry["evidence_ids"] = []
                        continue
                # A4 v6 返工（对照组修复）：旧草稿多字段条目——锚点字段已核实（与 quote 一致且 quote
                # 命中来源片段），其余进入正式检索的次要字段（例题推导/知识点适用条件/重点难点依据）
                # 须逐字段与该条目自身来源片段核对：能从该片段逐字核实的原样内容保留（进入发布版本与
                # 正式索引），无法核实的字段排除（清空）。核对范围锁定在确定来源片段本身，不跨片段。
                if is_legacy_draft:
                    for field in _BODY_FIELDS[list_key]:
                        if field == key_field:
                            continue
                        val = (entry.get(field) or "").strip()
                        if val and val not in seg_text:
                            entry[field] = ""
                span = EvidenceSpan(
                    transcript_version_id=note.transcript_version_id,
                    segment_start=seq,
                    segment_end=seq,
                    start_ms=seg["start_ms"],
                    end_ms=seg["end_ms"],
                    quote=quote,
                    is_valid=True,
                )
                db.add(span)
                await db.flush()
                db.add(
                    ArtifactEvidence(
                        artifact_type="note_version",
                        artifact_id=note.id,
                        evidence_id=span.id,
                    )
                )
                await db.flush()
                entry["evidence_ids"] = [str(span.id)]
                if entry.get("status") not in ("confirmed", "pending"):
                    entry["status"] = "confirmed"
        # 章节正文与结论保持一致：保留用户确认的章节数量、标题、顺序与要点归属（A4 返工 v4），
        # 仅按重新核对后的 key_conclusions 更新各节 points 的证据状态，不再坍缩为单章。
        reverified_kcs = {
            ((e.get("text") or "").strip() if isinstance(e, dict) else ""): e
            for e in (content.get("key_conclusions") or [])
            if isinstance(e, dict)
        }
        sections = content.get("sections") or []
        new_sections = []
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            points = []
            for p in (sec.get("points") or []):
                if not isinstance(p, dict):
                    continue
                key = (p.get("text") or "").strip()
                target = reverified_kcs.get(key)
                if target is None:
                    # 正文变化（text 改变）→ 按旧证据 id 归属到重新核对后的结论（避免章节残留旧文）。
                    for eid in (p.get("evidence_ids") or []):
                        candidates = old_evidence_to_kc.get(str(eid)) or []
                        if candidates:
                            target = candidates[0]
                            break
                points.append(target or p)
            new_sections.append({"heading": sec.get("heading"), "points": points})
        content["sections"] = new_sections if new_sections else [
            {"heading": "要点", "points": content.get("key_conclusions", [])}
        ]
        # JSONB 列对嵌套结构就地变更不做脏标记，须显式标记才能在提交时落库（A4 返工）。
        flag_modified(note, "content")
        await db.flush()

    async def _publish_note_if_current(
        self, db: AsyncSession, note: NoteVersion, lecture_id: uuid.UUID, transcript_version_id: uuid.UUID
    ) -> bool:
        meeting = await db.get(Meeting, lecture_id)
        if meeting is None:
            return False
        # 条件切换：仅当本笔记来源转写仍是当前有效输入时，才切换当前笔记指针；
        # 晚完成的旧输入整理不覆盖已发布的新输入笔记（P03-11）。
        if meeting.active_transcript_version_id is not None and meeting.active_transcript_version_id != transcript_version_id:
            note.status = "draft"
            await db.flush()
            return False
        if meeting.active_note_version_id is not None and meeting.active_note_version_id != note.id:
            old = await db.get(NoteVersion, meeting.active_note_version_id)
            if old is not None:
                old.status = "superseded"
        meeting.active_note_version_id = note.id
        note.status = "published"
        note.published_at = _now()
        # 索引交接意向（幂等：唯一 note_version_id）
        existing_intent = (
            await db.execute(
                select(IndexIntent).where(IndexIntent.note_version_id == note.id)
            )
        ).scalars().first()
        if existing_intent is None:
            db.add(
                IndexIntent(
                    note_version_id=note.id,
                    source_version_id=note.transcript_version_id,
                    status="pending",
                )
            )
        await db.flush()
        if _publish_hook is not None:
            _publish_hook()
        return True

    async def _mark_failure(
        self,
        db: AsyncSession,
        job: ProcessingJob,
        e: ModelError,
        owner: str | None = None,
        epoch: int = 0,
    ) -> bool:
        """执行者触发的失败/取消终态写入，携带 owner/epoch 受租约保护（CA-11）。"""

        def mutate(row: ProcessingJob) -> None:
            row.error_type = e.error_type
            row.error_code = e.code
            if row.cancel_requested:
                row.status = "cancelled"
            else:
                row.status = "failed"

        if owner:
            return await lease_service.mutate_status_if_owner(db, job.id, owner, epoch, mutate)
        mutate(job)
        await db.commit()
        return True

    async def _reload_job(self, db: AsyncSession, job_id: uuid.UUID) -> ProcessingJob | None:
        """回滚后重新读取任务（失去租约/未写入时返回当前最新状态，不改写）。"""
        await db.rollback()
        job = await db.get(ProcessingJob, job_id)
        if job is not None:
            await db.refresh(job)
        return job

    # ── 读取 / 编辑 / 发布 ────────────────────────────────────────────
    async def _get_note_owned(self, db: AsyncSession, scope: ScopeContext, note_id: uuid.UUID) -> NoteVersion:
        note = await db.get(NoteVersion, note_id)
        if note is None:
            raise ServiceError(404, "not_found", "笔记不存在", retryable=False)
        await get_accessible_lecture(db, scope, note.lecture_id)
        return note

    async def get_notes(self, db: AsyncSession, scope: ScopeContext, lecture_id: uuid.UUID) -> list[NoteVersion]:
        await get_accessible_lecture(db, scope, lecture_id)
        rows = (
            await db.execute(
                select(NoteVersion)
                .where(NoteVersion.lecture_id == lecture_id)
                .order_by(NoteVersion.created_at.desc())
            )
        ).scalars().all()
        return list(rows)

    async def get_note(self, db: AsyncSession, scope: ScopeContext, note_id: uuid.UUID) -> NoteVersion:
        return await self._get_note_owned(db, scope, note_id)

    async def get_current_note(self, db: AsyncSession, scope: ScopeContext, lecture_id: uuid.UUID) -> NoteVersion | None:
        meeting = await get_accessible_lecture(db, scope, lecture_id)
        if meeting.active_note_version_id is None:
            return None
        note = await db.get(NoteVersion, meeting.active_note_version_id)
        if note is None or note.lecture_id != lecture_id:
            return None
        return note

    async def edit_note(
        self, db: AsyncSession, scope: ScopeContext, note_id: uuid.UUID, patch: dict
    ) -> NoteVersion:
        note = await self._get_note_owned(db, scope, note_id)
        if note.status not in ("draft", "pending"):
            raise ServiceError(409, "invalid_state", "仅草稿/待确认笔记可编辑", retryable=False)
        old_content = dict(note.content or {})
        content = dict(note.content or {})
        for key in (
            "title", "outline", "sections", "key_conclusions",
            "knowledge_points", "examples", "key_points",
        ):
            if key in patch:
                entries = patch[key]
                # A4 v5：客户端可能回传内部 `_verified_body`（伪造基线）——不可作为核实依据，
                # 一律剥离；服务端可信基线仅由 _preserve_verified_baselines 从 old_content 带过。
                if isinstance(entries, list):
                    for e in entries:
                        if isinstance(e, dict):
                            e.pop("_verified_body", None)
                content[key] = entries
        # A4 v4：编辑接口整体替换条目列表时，客户端通常只提交业务字段，会丢失内部的
        # `_verified_body` 正文基线。按稳定身份（锚点键 / 证据 id / quote）把旧条目基线
        # 带过来，使「只改正文、保留原引文和证据」的实质变化在发布时仍可被识别。
        self._preserve_verified_baselines(content, old_content)
        note.content = content
        note.edit_source = patch.get("edit_source") or {"kind": "user_edit"}
        note.edited_at = _now()
        await db.commit()
        await db.refresh(note)
        return note

    @staticmethod
    def _preserve_verified_baselines(content: dict, old_content: dict) -> None:
        """编辑整体替换条目列表后，把旧条目的 ``_verified_body`` 正文基线带过来。

        匹配优先级：锚点键（正文未变）→ 证据 id（只改正文保留证据）→ quote（保留引文）。
        三者都不匹配（全新条目）则无基线，发布时由服务端重建（见 ``_reverify_note_evidence``）。
        """
        for list_key, anchor in _ANCHOR_FIELDS.items():
            new_entries = content.get(list_key)
            if not isinstance(new_entries, list):
                continue
            old_entries = (old_content or {}).get(list_key) or []
            by_anchor: dict[str, list[dict]] = {}
            by_evidence: dict[str, list[dict]] = {}
            by_quote: dict[str, list[dict]] = {}
            for old in old_entries:
                if not isinstance(old, dict):
                    continue
                by_anchor.setdefault((old.get(anchor) or "").strip(), []).append(old)
                for eid in (old.get("evidence_ids") or []):
                    by_evidence.setdefault(str(eid), []).append(old)
                q = (old.get("quote") or "").strip()
                if q:
                    by_quote.setdefault(q, []).append(old)
            for entry in new_entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("_verified_body") is not None:
                    continue  # 已有基线（客户端回传完整内容）则不动
                matched = None
                a = (entry.get(anchor) or "").strip()
                for old in by_anchor.get(a, []):
                    if old.get("_verified_body") is not None:
                        matched = old
                        break
                if matched is None:
                    for eid in (entry.get("evidence_ids") or []):
                        for old in by_evidence.get(str(eid), []):
                            if old.get("_verified_body") is not None:
                                matched = old
                                break
                        if matched is not None:
                            break
                if matched is None:
                    q = (entry.get("quote") or "").strip()
                    for old in by_quote.get(q, []):
                        if old.get("_verified_body") is not None:
                            matched = old
                            break
                if matched is not None:
                    entry["_verified_body"] = matched["_verified_body"]

    async def publish_note(self, db: AsyncSession, scope: ScopeContext, note_id: uuid.UUID) -> NoteVersion:
        note = await self._get_note_owned(db, scope, note_id)
        if note.status not in ("draft", "pending"):
            return note
        if note.transcript_version_id is None:
            raise ServiceError(422, "invalid_input", "笔记缺少来源版本，无法发布", retryable=False)
        # 缺块草稿不得正式发布（P03-12）：覆盖不完整时拒绝，不切当前指针、不生成索引意向。
        coverage = (note.content or {}).get("coverage") or {}
        if coverage.get("complete") is not True:
            raise ServiceError(409, "incomplete_coverage", "覆盖不完整，禁止正式发布", retryable=False)
        # A4 返工：发布前服务端重新核对编辑后的正文与来源，失配/伪造条目降待确认并排除出正式知识。
        await self._reverify_note_evidence(db, note)
        await self._publish_note_if_current(db, note, note.lecture_id, note.transcript_version_id)
        await db.commit()
        await db.refresh(note)
        return note

    async def get_run_detail(self, db: AsyncSession, job_id: uuid.UUID) -> dict:
        """A6 运行详情：用作业 ID 查询四类分支的选择 / 执行状态 / 耗时 / 错误 / 重试 / 用量，
        覆盖跳过、成功、超时与预算阻止。

        共享一次 ``extract`` 调用的四个分支展示同一调用标识（``organize:extract:{job}:{chunk}``），
        避免重复记账；供应商未返回用量标 ``usage_state`` 为 unknown（不冒充零调用）。
        """
        job = await db.get(ProcessingJob, job_id)
        if job is None:
            raise ServiceError(404, "not_found", "作业不存在", retryable=False)

        def _iso(dt):
            return dt.isoformat() if dt is not None else None

        def _elapsed(start, finish):
            if start is not None and finish is not None:
                return int((finish - start).total_seconds() * 1000)
            return None

        # 规划决定的选择（预算阻止 / 超时等无笔记终态也能查）
        plan_cp = await get_checkpoint(db, job_id, "organize.plan")
        plan = (plan_cp.output_data if plan_cp is not None else None) or {}
        selected = _selected_branch_names(plan)

        chunks = (
            await db.execute(
                select(ChunkItem)
                .join(ChunkManifest, ChunkItem.manifest_id == ChunkManifest.id)
                .where(ChunkManifest.job_id == job_id)
                .order_by(ChunkItem.chunk_index)
            )
        ).scalars().all()

        calls = (
            await db.execute(
                select(ProviderCall)
                .where(ProviderCall.job_id == job_id)
                .order_by(ProviderCall.operation_key, ProviderCall.attempt_no)
            )
        ).scalars().all()

        extract_calls = [c for c in calls if c.operation_type == "organize_extract"]
        # 实际是否外发过 extract：网关路径有 ProviderCall（含失败/超时）；非网关路径无
        # ProviderCall，用块是否到达真实终态（成功或 error_code 非空）判定（预算阻止不出网）。
        non_gw_invoked = any(
            (c.attempt or 0) > 0 and (c.status == "succeeded" or c.error_code is not None)
            for c in chunks
        )
        invoked = bool(extract_calls) or non_gw_invoked
        executed_branches = selected if invoked else set()

        # 每分支 count：优先取最终笔记记录，否则回退到成功块输出汇总
        note = await db.get(NoteVersion, job.output_version) if job.output_version else None
        final_branches = ((note.content or {}).get("branches") if note is not None else None) or {}
        counts: dict[str, int] = {name: 0 for name in BRANCH_NAMES}
        if final_branches:
            for name in BRANCH_NAMES:
                b = final_branches.get(name)
                counts[name] = b.get("count", 0) if isinstance(b, dict) else 0
        else:
            for c in chunks:
                if c.status == "succeeded" and isinstance(c.output, dict):
                    counts["note"] += len(c.output.get("conclusions") or [])
                    counts["knowledge_point"] += len(c.output.get("knowledge_points") or [])
                    counts["example"] += len(c.output.get("examples") or [])
                    counts["key_point"] += len(c.output.get("key_points") or [])

        branch_detail = {}
        for name in BRANCH_NAMES:
            is_selected = name in selected
            branch_detail[name] = {
                "selected": is_selected,
                "skipped": not is_selected,
                "executed": name in executed_branches,
                "count": counts.get(name, 0),
            }

        chunk_detail = []
        for c in chunks:
            chunk_detail.append({
                "chunk_index": c.chunk_index,
                "status": c.status,
                "attempt": c.attempt or 0,
                "error_code": c.error_code,
                "started_at": _iso(c.started_at),
                "finished_at": _iso(c.finished_at),
                "elapsed_ms": _elapsed(c.started_at, c.finished_at),
                "call_key": f"organize:extract:{job_id}:{c.chunk_index}",
                "shared_branches": sorted(selected) if invoked else [],
            })

        by_key: dict[str, list[ProviderCall]] = {}
        for c in calls:
            by_key.setdefault(c.operation_key, []).append(c)
        shared_calls = []
        for key, rows in by_key.items():
            latest = max(rows, key=lambda r: r.attempt_no or 0)
            shared_calls.append({
                "operation_key": key,
                "operation_type": latest.operation_type,
                "attempts": len(rows),
                "status": latest.status,
                "error_code": latest.error_code,
                "started_at": _iso(latest.started_at),
                "finished_at": _iso(latest.finished_at),
                "elapsed_ms": _elapsed(latest.started_at, latest.finished_at),
                "usage_state": latest.usage_state,
                "usage_known": latest.usage is not None,
                "reserved_amount": latest.reserved_amount,
                "settled_amount": latest.settled_amount,
                "usage": latest.usage,
                "shared_branches": sorted(selected) if latest.operation_type == "organize_extract" else None,
            })

        provider_calls = [
            {
                "operation_key": c.operation_key,
                "attempt_no": c.attempt_no,
                "operation_type": c.operation_type,
                "provider": c.provider,
                "model": c.model,
                "status": c.status,
                "error_code": c.error_code,
                "started_at": _iso(c.started_at),
                "finished_at": _iso(c.finished_at),
                "elapsed_ms": _elapsed(c.started_at, c.finished_at),
                "usage": c.usage,
                "usage_known": c.usage is not None,
                "reserved_amount": c.reserved_amount,
                "settled_amount": c.settled_amount,
                "usage_state": c.usage_state,
            }
            for c in calls
        ]

        return {
            "job_id": str(job_id),
            "status": job.status,
            "stage": job.stage,
            "error_code": job.error_code,
            "error_type": job.error_type,
            "plan": plan,
            "branches": branch_detail,
            "chunks": chunk_detail,
            "shared_calls": shared_calls,
            "provider_calls": provider_calls,
        }

    async def recover_and_dispatch_organize(self, db: AsyncSession, model=None) -> dict:
        """恢复失联 / 到期重试 / 派发 queued 的整理任务（至少一次投递）。"""
        stats = {"recovered": 0, "dispatched": 0, "succeeded": 0, "failed": 0,
                 "retry_wait": 0, "cancelled": 0}

        # 回退只读（CA-25）：停写期不领取、不派发、不外发、不发布整理任务。
        from app.services.access import is_read_only

        if is_read_only():
            stats["read_only"] = True
            return stats

        from datetime import timedelta

        stale_cutoff = _now() - timedelta(seconds=settings.JOB_HEARTBEAT_TIMEOUT_SECONDS)
        stale = (
            await db.execute(
                select(ProcessingJob).where(
                    ProcessingJob.type == "organize",
                    ProcessingJob.status == "running",
                    ProcessingJob.heartbeat_at.is_not(None),
                    ProcessingJob.heartbeat_at < stale_cutoff,
                )
            )
        ).scalars().all()
        for job in stale:
            job.status = "queued"
            stats["recovered"] += 1

        due = (
            await db.execute(
                select(ProcessingJob).where(
                    ProcessingJob.type == "organize",
                    ProcessingJob.status == "retry_wait",
                    ProcessingJob.retry_at.is_not(None),
                    ProcessingJob.retry_at <= _now(),
                )
            )
        ).scalars().all()
        for job in due:
            job.status = "queued"
            stats["recovered"] += 1

        if stale or due:
            await db.commit()

        queued = (
            await db.execute(
                select(ProcessingJob)
                .where(ProcessingJob.type == "organize", ProcessingJob.status == "queued")
                .order_by(ProcessingJob.created_at.asc())
            )
        ).scalars().all()
        for job in queued:
            outbox = (
                await db.execute(
                    select(OutboxEvent).where(OutboxEvent.job_id == job.id)
                )
            ).scalars().first()
            if outbox is not None and outbox.status == "pending":
                outbox.status = "dispatched"
                outbox.dispatched_at = _now()
                outbox.attempt = (outbox.attempt or 0) + 1
                await db.commit()
            stats["dispatched"] += 1
            await self.run_organize_job(db, job.id, model=model)
            final = await db.get(ProcessingJob, job.id)
            if final is not None:
                key = final.status if final.status in stats else "failed"
                stats[key] += 1
        return stats


organize_service = OrganizeService()
