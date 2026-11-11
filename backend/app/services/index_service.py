"""检索索引服务（第四阶段）

从完整有效转写、已发布笔记、课件原文构建持久索引版本，并原子切换到有效索引：
- 仅索引完整有效转写（覆盖到末尾，不只摘要）、已发布笔记（排除草稿/失败/演示来源）、
  解析成功的课件（排除空文本/不支持格式）。
- 索引块保留来源身份（source_type + 版本 + 定位），embedding 与内容/模型/维度绑定。
- 原子切换：新版本 ready 后才激活，旧 active → superseded；失败不破坏当前版本。
- 消费第三阶段 IndexIntent：发布笔记触发索引，幂等、可恢复、重复交付不重复块。

任务沿用持久 job（type="index"，resource_id=course_id，output_version=index_version_id），
重复执行 / 恢复沿用已提交块，不重复写索引块或有效索引。
"""

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audio_asset import AudioAsset
from app.models.course import Course
from app.models.index_version import IndexBlock, IndexVersion
from app.models.meeting import Meeting
from app.models.note_version import NoteVersion
from app.models.organize import IndexIntent
from app.models.parsed_block import ParsedBlock as ParsedBlockModel
from app.models.processing_job import ProcessingJob, OutboxEvent, JobCheckpoint
from app.models.source_document import SourceDocument, SourceDocumentVersion
from app.models.transcript_version import TranscriptSegment, TranscriptVersion
from app.services.access import get_accessible_course, get_accessible_lecture
from app.services import lease_service
from app.services import rate_limit_service
from app.services.ai_call_gateway import ControlWaitError, json_deserializer, json_serializer
from app.services.checkpoint_service import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointCorrupt,
    get_checkpoint,
    mark_checkpoint_reused,
    mark_checkpoint_succeeded,
    norm_json,
    policy_version,
    sha256_hex,
)
from app.services.document_chunker import document_chunker
from app.services.document_parser import ParsedBlock
from app.services.embedding_service import EmbeddingProvider, get_classroom_embedding
from app.services.errors import BudgetExhaustedError, CallCancelled, ProviderUnknownError, ServiceError
from app.services.scope import ScopeContext

logger = logging.getLogger(__name__)

# 测试注入：构建首块提交后 / 激活切换提交前抛异常。生产恒为 None。
_build_hook = None
_activate_hook = None


class BuildInterrupted(Exception):
    """模拟 worker 失联：已提交块保留，恢复沿用。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _reload_job(db: AsyncSession, job_id: uuid.UUID) -> ProcessingJob | None:
    """回滚后重新读取任务（失去租约/未写入时返回当前最新状态，不改写）。"""
    await db.rollback()
    job = await db.get(ProcessingJob, job_id)
    if job is not None:
        await db.refresh(job)
    return job


def _index_config(embed: EmbeddingProvider) -> dict:
    return {
        "embedding_model": embed.model,
        "embedding_dimensions": embed.dimensions,
        "content_hash_algo": settings.INDEX_CONTENT_HASH_ALGO,
        "chunk_size": settings.INDEX_CHUNK_SIZE,
        "chunk_overlap": settings.INDEX_CHUNK_OVERLAP,
        "candidate_pool": settings.INDEX_CANDIDATE_POOL,
        "top_k": settings.INDEX_TOP_K,
        "rrf_k": settings.INDEX_RRF_K,
        "context_budget": settings.INDEX_CONTEXT_BUDGET,
        "single_source_limit": settings.INDEX_SINGLE_SOURCE_LIMIT,
        "relevance_threshold": settings.INDEX_RETRIEVAL_RELEVANCE_THRESHOLD,
        "lag_strategy": settings.INDEX_LAG_STRATEGY,
        "max_attempts": settings.INDEX_MAX_ATTEMPTS,
    }


def _default_embed() -> EmbeddingProvider:
    return get_classroom_embedding()


def _unit_key(u: dict) -> str:
    """索引单元稳定身份：来源类型 + 版本 + 块定位 + 内容 hash（CA-09，不用序号）。"""
    return sha256_hex(norm_json({
        "source_type": u["source_type"],
        "source_version_id": str(u["source_version_id"]),
        "block_id": u.get("block_id"),
        "page_no": u.get("page_no"),
        "segment_start": u.get("segment_start"),
        "segment_end": u.get("segment_end"),
        "content": u["content"],
    }))


def _serialize_units(units: list[dict]) -> list[dict]:
    """来源快照落库：UUID → 字符串，供 JSONB 持久化。"""
    out: list[dict] = []
    for u in units:
        d = dict(u)
        for k in ("source_version_id", "lecture_id", "course_id"):
            if d.get(k) is not None:
                d[k] = str(d[k])
        out.append(d)
    return out


def _deserialize_units(units: list[dict]) -> list[dict]:
    """恢复来源快照：字符串 → UUID。"""
    out: list[dict] = []
    for u in units:
        d = dict(u)
        for k in ("source_version_id", "lecture_id", "course_id"):
            if d.get(k) is not None:
                d[k] = uuid.UUID(d[k])
        out.append(d)
    return out


def _index_manifest_hash(units: list[dict]) -> str:
    """来源快照身份：冻结来源清单的规范化摘要（CA-09 输入身份）。"""
    return sha256_hex(norm_json(_serialize_units(units)))


class IndexService:
    # ── 来源收集 ──────────────────────────────────────────────────────
    async def _lectures(self, db: AsyncSession, course_id: uuid.UUID) -> list[Meeting]:
        rows = (
            await db.execute(
                select(Meeting)
                .where(Meeting.course_id == course_id)
                .order_by(Meeting.id)
            )
        ).scalars().all()
        return list(rows)

    async def _transcript_units(self, db: AsyncSession, course_id: uuid.UUID) -> list[dict]:
        units: list[dict] = []
        lectures = await self._lectures(db, course_id)
        for lecture in lectures:
            vid = lecture.active_transcript_version_id
            if vid is None:
                continue
            version = await db.get(TranscriptVersion, vid)
            if version is None or version.status not in ("active", "ready"):
                continue
            asset = await db.get(AudioAsset, version.audio_asset_id)
            if asset is not None and asset.is_demo:
                continue  # 演示来源不作正式召回
            segs = (
                await db.execute(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.version_id == vid)
                    .order_by(TranscriptSegment.seq_index)
                )
            ).scalars().all()
            if not segs:
                continue
            # 完整转写覆盖到末尾：按序分组为块（单片段原子），不丢末尾。
            current: list = []
            current_len = 0
            for s in segs:
                text = s.content or ""
                if current and current_len + len(text) > settings.INDEX_CHUNK_SIZE:
                    units.append(self._seg_unit(lecture, version, current))
                    current = [s]
                    current_len = len(text)
                else:
                    current.append(s)
                    current_len += len(text)
            if current:
                units.append(self._seg_unit(lecture, version, current))
        return units

    @staticmethod
    def _seg_unit(lecture, version, segs) -> dict:
        return {
            "source_type": "transcript",
            "source_version_id": version.id,
            "lecture_id": lecture.id,
            "course_id": lecture.course_id,
            "segment_start": segs[0].seq_index,
            "segment_end": segs[-1].seq_index,
            "content": "\n".join(s.content or "" for s in segs),
            "block_id": None,
            "page_no": None,
            "heading": None,
            "metadata": {"provider": version.provider},
        }

    async def _document_units(self, db: AsyncSession, course_id: uuid.UUID) -> list[dict]:
        units: list[dict] = []
        docs = (
            await db.execute(
                select(SourceDocument)
                .where(SourceDocument.course_id == course_id)
                .order_by(SourceDocument.id)
            )
        ).scalars().all()
        for doc in docs:
            version = (
                await db.execute(
                    select(SourceDocumentVersion)
                    .where(
                        SourceDocumentVersion.source_document_id == doc.id,
                        SourceDocumentVersion.status == "active",
                        SourceDocumentVersion.parse_status == "parsed",
                    )
                    .order_by(SourceDocumentVersion.version.desc())
                )
            ).scalars().first()
            if version is None:
                continue
            blocks = (
                await db.execute(
                    select(ParsedBlockModel)
                    .where(ParsedBlockModel.document_version_id == version.id)
                    .order_by(ParsedBlockModel.seq)
                )
            ).scalars().all()
            chunker_blocks = [
                ParsedBlock(
                    block_id=b.block_id,
                    kind=b.kind,
                    text=b.text,
                    heading=b.heading,
                    page_no=b.page_no,
                    paragraph_index=b.paragraph_index,
                    table_index=b.table_index,
                )
                for b in blocks
            ]
            for u in document_chunker.chunk_parsed_blocks(chunker_blocks):
                units.append(
                    {
                        "source_type": "document",
                        "source_version_id": version.id,
                        "lecture_id": doc.lecture_id,
                        "course_id": doc.course_id,
                        "block_id": u["block_id"],
                        "page_no": u["page_no"],
                        "heading": u["heading"],
                        "content": u["content"],
                        "segment_start": None,
                        "segment_end": None,
                        "metadata": {"paragraph_index": u["paragraph_index"],
                                     "table_index": u["table_index"]},
                    }
                )
        return units

    async def _note_units(self, db: AsyncSession, course_id: uuid.UUID) -> list[dict]:
        units: list[dict] = []
        lectures = await self._lectures(db, course_id)
        lecture_ids = [l.id for l in lectures]
        if not lecture_ids:
            return units
        notes = (
            await db.execute(
                select(NoteVersion)
                .where(
                    NoteVersion.lecture_id.in_(lecture_ids),
                    NoteVersion.status == "published",
                )
                .order_by(NoteVersion.id)
            )
        ).scalars().all()
        for note in notes:
            content = self._note_text(note)
            if not content.strip():
                continue
            # 已发布笔记保留原确认状态与证据关联（写入 metadata，供来源打开回听）
            evidence_ids: list[str] = self._note_evidence_ids(note)
            units.append(
                {
                    "source_type": "note",
                    "source_version_id": note.id,
                    "lecture_id": note.lecture_id,
                    "course_id": course_id,
                    "block_id": None,
                    "page_no": None,
                    "heading": None,
                    "content": content,
                    "segment_start": None,
                    "segment_end": None,
                    "metadata": {
                        "note_evidence_ids": evidence_ids,
                        "note_status": note.status,
                    },
                }
            )
        return units

    @staticmethod
    def _note_evidence_ids(note: NoteVersion) -> list[str]:
        # 仅「已核对（confirmed）」条目进入正式知识来源；待确认（pending）条目证据不计入。
        ids: list[str] = []
        c = note.content or {}
        for branch in ("key_conclusions", "knowledge_points", "examples", "key_points"):
            for item in c.get(branch, []):
                if item.get("status") != "confirmed":
                    continue
                ids.extend(item.get("evidence_ids") or [])
        return ids

    @staticmethod
    def _note_text(note: NoteVersion) -> str:
        # 失效引用被降「待确认（pending）」的条目须从正式知识中排除（A4/R4）：仅索引 confirmed。
        c = note.content or {}
        parts: list[str] = []
        if c.get("title"):
            parts.append(c["title"])
        for s in c.get("sections", []):
            if s.get("heading"):
                parts.append(s["heading"])
            for p in s.get("points", []):
                if p.get("text") and p.get("status") == "confirmed":
                    parts.append(p["text"])
        for kc in c.get("key_conclusions", []):
            if kc.get("text") and kc.get("status") == "confirmed":
                parts.append(kc["text"])
        for kp in c.get("knowledge_points", []):
            if kp.get("status") != "confirmed":
                continue
            if kp.get("definition"):
                parts.append(kp["definition"])
            if kp.get("applicability"):
                parts.append(kp["applicability"])
        for ex in c.get("examples", []):
            if ex.get("status") != "confirmed":
                continue
            if ex.get("problem"):
                parts.append(ex["problem"])
            if ex.get("derivation"):
                parts.append(ex["derivation"])
        for pt in c.get("key_points", []):
            if pt.get("status") != "confirmed":
                continue
            if pt.get("point"):
                parts.append(pt["point"])
            if pt.get("reason"):
                parts.append(pt["reason"])
        return "\n".join(parts)

    async def gather_sources(self, db: AsyncSession, course_id: uuid.UUID) -> list[dict]:
        """收集三类来源的索引单元（完整转写 + 课件 + 已发布笔记）。"""
        units: list[dict] = []
        units += await self._transcript_units(db, course_id)
        units += await self._document_units(db, course_id)
        units += await self._note_units(db, course_id)
        return units

    # ── 索引构建 ──────────────────────────────────────────────────────
    async def _check_dimensions(self, embed: EmbeddingProvider) -> None:
        if embed.dimensions != settings.INDEX_EMBEDDING_DIMENSIONS:
            raise ServiceError(
                422, "invalid_embedding_dimension",
                f"embedding 维度 {embed.dimensions} 与索引维度 "
                f"{settings.INDEX_EMBEDDING_DIMENSIONS} 不一致",
                retryable=False,
            )

    async def build_index(
        self,
        db: AsyncSession,
        course_id: uuid.UUID,
        *,
        embed: EmbeddingProvider | None = None,
        index_version: IndexVersion | None = None,
        units: list[dict] | None = None,
        job: ProcessingJob | None = None,
        owner: str | None = None,
        epoch: int = 0,
        subject_id: str | None = None,
    ) -> IndexVersion:
        """构建课程索引版本；index_version 传入时续写（恢复沿用已提交块）。

        有 ``job`` 时按稳定单元键（CA-09）复用已 embed 单元；``units`` 传冻结来源快照，
        恢复时禁止重新 gather_sources。无 ``job``（IndexIntent 旧路径）保持按序续写。
        ``owner``/``epoch`` 非空时对外发向量化前后做归属隔离（CA-11）。
        ``subject_id`` 用于网关预算主体（默认本地主体）。
        """
        embed = embed or _default_embed()
        await self._check_dimensions(embed)

        if units is None:
            units = await self.gather_sources(db, course_id)

        if index_version is None:
            index_version = IndexVersion(
                course_id=course_id,
                status="building",
                config=_index_config(embed),
                source_count=0,
                block_count=0,
            )
            db.add(index_version)
            await db.flush()

        # 已嵌入单元：job 路径按稳定键；否则按 chunk_index（S3 前旧路径）。
        if job is not None:
            embedded = {
                cp.unit_key: cp
                for cp in (
                    await db.execute(
                        select(JobCheckpoint).where(
                            JobCheckpoint.job_id == job.id,
                            JobCheckpoint.step_key == "index.embed",
                            JobCheckpoint.status == "succeeded",
                        )
                    )
                ).scalars().all()
            }
            existing_chunks = set()
        else:
            embedded = {}
            existing_chunks = {
                row[0]
                for row in (
                    await db.execute(
                        select(IndexBlock.chunk_index).where(
                            IndexBlock.index_version_id == index_version.id
                        )
                    )
                ).all()
            }

        first_committed = False
        for chunk_index, u in enumerate(units):
            key = _unit_key(u)
            if job is not None:
                if key in embedded:
                    # 复用成功单元前核对引用（output_ref → 块）与摘要/schema（CA-02）
                    if not await self._unit_ref_intact(db, embedded[key], key, u["content"]):
                        raise CheckpointCorrupt()
                    # 复用已向量化单元：跳过 embed 外发，记录复用次数（CA-24）
                    await mark_checkpoint_reused(db, job.id)
                    continue
            elif chunk_index in existing_chunks:
                continue
            # 外发向量化前核对归属（CA-11）
            if owner and job is not None and not await lease_service.assert_owner(db, job.id, owner, epoch):
                raise lease_service.StaleLeaseError()
            if settings.CALL_GATEWAY_ENABLED:
                # 网关路径（S3）：embed 经统一网关受控计费（任务路径与 IndexIntent 路径同源）。
                from app.services.ai_call_gateway import CallSpec, call_controlled

                provider_name = getattr(embed, "name", None) or getattr(embed, "model", "embed")
                op_key = (
                    f"index:embed:{job.id}:{key}" if job is not None
                    else f"index:intent:{course_id}:{key}"
                )
                spec = CallSpec(
                    operation_key=op_key,
                    operation_type="index_embed",
                    operation_family=rate_limit_service.FAMILY_EMBED,
                    provider=provider_name,
                    model=getattr(embed, "model", None),
                    job_id=job.id if job is not None else None,
                    checkpoint_id=job.id if job is not None else None,
                    subject_id=subject_id,
                    is_idempotent=False,
                    owner=owner,
                    epoch=epoch,
                    result_serializer=json_serializer,
                    result_deserializer=json_deserializer,
                )
                outcome = await call_controlled(
                    db, spec, lambda: embed.embed_batch([u["content"]])
                )
                if outcome.kind == "success":
                    vectors = outcome.result
                    vector = vectors[0] if vectors else None
                elif outcome.kind == "unknown":
                    raise ProviderUnknownError("embedding 结果未知，阻断普通重试")
                elif outcome.error is not None:
                    raise outcome.error
                else:
                    raise ServiceError(422, "embedding_failed",
                                       outcome.error_code or "embedding_failed", retryable=False)
            else:
                # embedding 共享桶（CA-16）：仅任务路径受控；IndexIntent 旧路径在 S3 接入统一网关。
                if job is not None:
                    await rate_limit_service.consume_provider_bucket(
                        rate_limit_service.FAMILY_EMBED,
                        getattr(embed, "name", None) or getattr(embed, "model", "embed"),
                    )
                vector = (await embed.embed_batch([u["content"]]))[0]
            if vector is None:
                raise ServiceError(422, "embedding_failed", "向量化失败", retryable=False)
            # 外发响应返回后、提交单元前：加行锁重新核对归属（CA-11）
            if owner and job is not None and not await lease_service.assert_owner(db, job.id, owner, epoch, for_update=True):
                raise lease_service.StaleLeaseError()
            block = IndexBlock(
                index_version_id=index_version.id,
                course_id=u["course_id"],
                lecture_id=u["lecture_id"],
                source_type=u["source_type"],
                source_transcript_version_id=(
                    u["source_version_id"] if u["source_type"] == "transcript" else None
                ),
                source_document_version_id=(
                    u["source_version_id"] if u["source_type"] == "document" else None
                ),
                source_note_version_id=(
                    u["source_version_id"] if u["source_type"] == "note" else None
                ),
                block_id=u.get("block_id"),
                page_no=u.get("page_no"),
                segment_start=u.get("segment_start"),
                segment_end=u.get("segment_end"),
                content=u["content"],
                embedding=vector,
                metadata_=u.get("metadata"),
                chunk_index=chunk_index,
            )
            db.add(block)
            await db.flush()
            if job is not None:
                await mark_checkpoint_succeeded(
                    db,
                    job_id=job.id,
                    step_key="index.embed",
                    unit_key=key,
                    input_hash=_index_manifest_hash(units),
                    config_hash=job.config_hash,
                    output_ref=str(block.id),
                    output_hash=sha256_hex(norm_json({"content": u["content"], "key": key})),
                )
            # 分批提交：首块提交后即可恢复（P04-13）。
            await db.commit()
            if _build_hook is not None and not first_committed:
                first_committed = True
                _build_hook()

        index_version.block_count = len(units)
        index_version.source_count = len(units)
        index_version.status = "ready"
        await db.commit()
        await db.refresh(index_version)
        return index_version

    async def _unit_ref_intact(
        self, db: AsyncSession, cp: JobCheckpoint, key: str, content: str
    ) -> bool:
        """复用已成功 index.embed 单元前核对引用与摘要/schema（CA-02），不只看 status/key。"""
        if cp.output_hash is None or cp.output_ref is None:
            return False
        if cp.schema_version is not None and cp.schema_version != CHECKPOINT_SCHEMA_VERSION:
            return False
        if cp.output_hash != sha256_hex(norm_json({"content": content, "key": key})):
            return False
        block = await db.get(IndexBlock, uuid.UUID(cp.output_ref))
        if block is None or block.content != content:
            return False
        return True

    async def activate(self, db: AsyncSession, index_version: IndexVersion) -> None:
        """原子激活：旧 active → superseded，新 → active（切换提交前可注入失败）。

        旧构建晚完成不覆盖较新的有效索引：若传入版本早于当前 active（更早开始构建），
        仅标记为 superseded，不切 active。
        """
        old = (
            await db.execute(
                select(IndexVersion).where(
                    IndexVersion.course_id == index_version.course_id,
                    IndexVersion.status == "active",
                )
            )
        ).scalars().first()
        if old is not None and old.id != index_version.id:
            if (
                index_version.created_at is not None
                and old.created_at is not None
                and index_version.created_at < old.created_at
            ):
                index_version.status = "superseded"
                await db.commit()
                return
            old.status = "superseded"
            # 先提交旧 active → superseded，再置新 active，避免部分唯一索引
            # （course_id WHERE status='active'）在切换瞬间出现两个 active。
            await db.flush()
        index_version.status = "active"
        index_version.activated_at = _now()
        await db.flush()
        if _activate_hook is not None:
            _activate_hook()
        await db.commit()

    # ── 任务入口 ──────────────────────────────────────────────────────
    async def create_index_job(
        self, db: AsyncSession, scope: ScopeContext, course_id: uuid.UUID
    ) -> ProcessingJob:
        await get_accessible_course(db, scope, course_id)
        config_hash = hashlib.sha256(
            json.dumps(_index_config(_default_embed()), sort_keys=True, default=str).encode()
        ).hexdigest()[:32]
        existing = (
            await db.execute(
                select(ProcessingJob).where(
                    ProcessingJob.type == "index",
                    ProcessingJob.resource_id == course_id,
                    ProcessingJob.config_hash == config_hash,
                ).order_by(ProcessingJob.created_at.desc())
            )
        ).scalars().first()
        if existing is not None and existing.status == "succeeded":
            # 成功任务复用还须判断来源快照身份（CA-09）：来源变化后允许建立独立任务/快照，
            # 不能仅凭课程+配置复用旧成功任务。
            manifest_cp = await get_checkpoint(db, existing.id, "index.manifest")
            if manifest_cp is not None and manifest_cp.status == "succeeded":
                serialized = (manifest_cp.output_data or {}).get("units", [])
                if (
                    isinstance(serialized, list)
                    and manifest_cp.output_hash == sha256_hex(norm_json(serialized))
                    and manifest_cp.input_hash == _index_manifest_hash(await self.gather_sources(db, course_id))
                ):
                    return existing
            # 来源已变化或 manifest 缺失：不复用旧成功任务，新建独立任务/快照
        job = ProcessingJob(
            type="index",
            resource_id=course_id,
            input_version=course_id,
            config_hash=config_hash,
            subject_id=scope.subject_id,
            config_snapshot=_index_config(_default_embed()),
            policy_version=policy_version(),
            status="queued",
            stage="created",
            attempt=0,
            cancel_requested=False,
        )
        db.add(job)
        await db.flush()
        db.add(
            OutboxEvent(
                business_key=f"job:{job.id}",
                event_type="index_requested",
                job_id=job.id,
                payload={"job_id": str(job.id), "course_id": str(course_id)},
                status="pending",
                attempt=0,
            )
        )
        await db.commit()
        await db.refresh(job)
        return job

    async def run_index_job(
        self, db: AsyncSession, job_id: uuid.UUID, embed: EmbeddingProvider | None = None
    ) -> ProcessingJob | None:
        job = await db.get(ProcessingJob, job_id)
        if job is None or job.type != "index":
            return job
        if job.status == "succeeded":
            return job
        if job.cancel_requested:
            job.status = "cancelled"
            await db.commit()
            await db.refresh(job)
            return job

        embed = embed or _default_embed()

        # 租约原子领取（CA-11）：实际显式索引执行入口统一领取，他人有效持有则本次不处理。
        owner = lease_service.new_owner()
        claimed, epoch = await lease_service.claim_job(db, job_id, owner)
        if not claimed:
            await db.commit()
            await db.refresh(job)
            return job
        await db.commit()
        await db.refresh(job)

        # 首块提交前即持久化索引版本指针：worker 失联后恢复沿用同一版本，不重复建版本/块。
        if job.output_version is not None:
            index_version = await db.get(IndexVersion, job.output_version)
        else:
            index_version = IndexVersion(
                course_id=job.resource_id,
                status="building",
                config=_index_config(embed),
                source_count=0,
                block_count=0,
            )
            db.add(index_version)
            await db.flush()
            job.output_version = index_version.id
        await db.commit()

        # 长调用期间独立 session 周期续租（CA-11）：逐块向量化期间租约不因主 session 阻塞而过期。
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            lease_service.heartbeat_loop(job_id, owner, epoch, heartbeat_stop)
        )

        try:
            # 冻结来源快照（CA-09）：manifest 已成功则复用冻结清单，不重新 gather_sources；
            # 复用前核对 schema/配置身份与 units 摘要（CA-02），篡改 → checkpoint_corrupt。
            manifest_cp = await get_checkpoint(db, job.id, "index.manifest")
            if manifest_cp is not None and manifest_cp.status == "succeeded":
                if manifest_cp.schema_version != CHECKPOINT_SCHEMA_VERSION:
                    raise CheckpointCorrupt()
                if manifest_cp.config_hash is not None and manifest_cp.config_hash != job.config_hash:
                    raise CheckpointCorrupt()
                serialized = (manifest_cp.output_data or {}).get("units", [])
                if not isinstance(serialized, list):
                    raise CheckpointCorrupt()
                if manifest_cp.output_hash != sha256_hex(norm_json(serialized)):
                    raise CheckpointCorrupt()
                units = _deserialize_units(serialized)
                if manifest_cp.input_hash is not None and manifest_cp.input_hash != _index_manifest_hash(units):
                    raise CheckpointCorrupt()
            else:
                units = await self.gather_sources(db, job.resource_id)
                serialized = _serialize_units(units)
                await mark_checkpoint_succeeded(
                    db,
                    job_id=job.id,
                    step_key="index.manifest",
                    input_hash=_index_manifest_hash(units),
                    config_hash=job.config_hash,
                    output_data={"units": serialized},
                    output_hash=sha256_hex(norm_json(serialized)),
                )
                await db.commit()

            index_version = await self.build_index(
                db, job.resource_id, embed=embed, index_version=index_version,
                units=units, job=job, owner=owner, epoch=epoch, subject_id=job.subject_id,
            )
            # 激活切换前校验租约仍归本执行者（CA-11）：加行锁使校验与切换同事务原子
            if not await lease_service.assert_owner(db, job_id, owner, epoch, for_update=True):
                raise lease_service.StaleLeaseError()
            await self.activate(db, index_version)
            await mark_checkpoint_succeeded(
                db,
                job_id=job.id,
                step_key="index.activate",
                input_hash=_index_manifest_hash(units),
                config_hash=job.config_hash,
                output_ref=str(index_version.id),
                output_hash=sha256_hex(norm_json({"version": str(index_version.id)})),
            )
            job.output_version = index_version.id
            job.status = "succeeded"
            job.stage = "activated"
            job.last_successful_stage = "index.activate"
            job.error_code = None
            job.error_type = None
            await db.commit()
            await db.refresh(job)
            return job
        except BuildInterrupted:
            raise
        except CallCancelled:
            # 网关在再次外发前检测到取消（CA-23）：受租约保护标记 cancelled。
            def _cancel(row: ProcessingJob) -> None:
                row.status = "cancelled"
                row.error_code = "cancelled"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _cancel):
                return await _reload_job(db, job_id)
            await db.refresh(job)
            return job
        except lease_service.StaleLeaseError:
            # 租约已被接管/过期：不提交产物、不改写任务状态，交由新 owner 处理（CA-11）
            return await _reload_job(db, job_id)
        except rate_limit_service.RateLimitedError as e:
            # 供应商桶拒绝（CA-16）：落库 retry_wait 并释放租约，不消耗外部 attempt_no、不出网。
            if await rate_limit_service.mark_control_wait(
                db, job_id, owner, epoch, "rate_limited", e.retry_after_seconds
            ):
                return await _reload_job(db, job_id)
            return await _reload_job(db, job_id)
        except rate_limit_service.RateLimitUnavailableError:
            # Redis 不可用（CA-16）：worker 持久等待 ≥5s 再检查，禁止本地无限放行、禁止出网。
            if await rate_limit_service.mark_control_wait(
                db, job_id, owner, epoch, "rate_limit_unavailable", settings.CHECKPOINT_REDIS_WAIT_SECONDS
            ):
                return await _reload_job(db, job_id)
            return await _reload_job(db, job_id)
        except ControlWaitError as e:
            # 供应商在途 permit 已满（CA-17）：落库控制等待，不占 permit/预算/外部 attempt。
            if await rate_limit_service.mark_control_wait(
                db, job_id, owner, epoch, "concurrency_limit",
                e.wait_seconds or settings.CHECKPOINT_PROVIDER_PERMIT_TTL_SECONDS
            ):
                return await _reload_job(db, job_id)
            return await _reload_job(db, job_id)
        except BudgetExhaustedError:
            def _budget(row: ProcessingJob) -> None:
                row.error_type = "budget_exhausted"
                row.error_code = "budget_exhausted"
                row.status = "failed"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _budget):
                return await _reload_job(db, job_id)
            await db.refresh(job)
            return job
        except ProviderUnknownError:
            def _unknown_call(row: ProcessingJob) -> None:
                row.error_type = "provider_unknown"
                row.error_code = "provider_unknown"
                row.status = "failed"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _unknown_call):
                return await _reload_job(db, job_id)
            await db.refresh(job)
            return job
        except CheckpointCorrupt:
            # 已成功 Checkpoint 引用/摘要/schema 失效：明确失败不重试、不伪成功（CA-02）
            def _corrupt(row: ProcessingJob) -> None:
                row.error_type = "checkpoint_corrupt"
                row.error_code = "checkpoint_corrupt"
                row.status = "failed"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _corrupt):
                return await _reload_job(db, job_id)
            await db.refresh(job)
            return job
        except ServiceError as e:
            def _svc(row: ProcessingJob) -> None:
                row.error_type = e.code
                row.error_code = e.code
                row.status = "failed"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _svc):
                return await _reload_job(db, job_id)
            await db.refresh(job)
            return job
        except Exception as e:  # noqa: BLE001
            logger.exception("index job %s 处理异常", job.id)
            # 回滚未提交的块/激活/证据，再以受租约保护的短事务写终态（CA-11），
            # 迟到异常不得改写新 owner 状态。
            def _unknown(row: ProcessingJob) -> None:
                row.error_type = "provider_error"
                row.error_code = "unknown"
                row.status = "failed"

            if not await lease_service.mutate_status_if_owner(db, job_id, owner, epoch, _unknown):
                return await _reload_job(db, job_id)
            await db.refresh(job)
            return job
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ── 索引意向消费 ──────────────────────────────────────────────────
    async def consume_intent(
        self, db: AsyncSession, intent: IndexIntent, embed: EmbeddingProvider | None = None
    ) -> None:
        """消费第三阶段 IndexIntent：把已发布笔记索引进该课程当前 active 索引。

        幂等：已 consumed 直接返回；同一笔记版本已索引则不重复写块。
        构建失败不标 consumed（意向与真实产物一致）。
        """
        if intent.status == "consumed":
            return
        embed = embed or _default_embed()
        note = await db.get(NoteVersion, intent.note_version_id)
        if note is None or note.status != "published":
            # 笔记已删除/未发布：意向落空，标记失败而非伪成功
            intent.status = "failed"
            await db.commit()
            return

        lecture = await db.get(Meeting, note.lecture_id)
        if lecture is None or lecture.course_id is None:
            intent.status = "failed"
            await db.commit()
            return
        course_id = lecture.course_id

        # 网关预算主体：课程归属者（IndexIntent 与显式索引同受控计费，CA-10/CA-15）
        subject_id = settings.LOCAL_SUBJECT_ID
        course = await db.get(Course, course_id) if course_id is not None else None
        if course is not None and course.owner_id:
            subject_id = course.owner_id

        active = (
            await db.execute(
                select(IndexVersion).where(
                    IndexVersion.course_id == course_id, IndexVersion.status == "active"
                )
            )
        ).scalars().first()
        if active is None:
            # 无有效索引：新建并激活（该课程的首次索引）
            # An interrupted intent build has committed blocks but no active version.
            # Resume only when config AND every committed source position still match.
            resume = None
            units = await self.gather_sources(db, course_id)
            candidates = (await db.execute(select(IndexVersion).where(
                IndexVersion.course_id == course_id, IndexVersion.status == "building"
            ).order_by(IndexVersion.created_at.desc()))).scalars().all()
            for candidate in candidates:
                if candidate.config != _index_config(embed):
                    continue
                blocks = (await db.execute(select(IndexBlock).where(
                    IndexBlock.index_version_id == candidate.id
                ).order_by(IndexBlock.chunk_index))).scalars().all()
                matches = len(blocks) <= len(units)
                for position, block in enumerate(blocks):
                    if not matches:
                        break
                    unit = units[position]
                    source_id = (block.source_transcript_version_id or
                                 block.source_document_version_id or block.source_note_version_id)
                    matches = (block.chunk_index == position and block.content == unit["content"]
                               and block.source_type == unit["source_type"]
                               and source_id == unit["source_version_id"]
                               and block.block_id == unit.get("block_id")
                               and block.segment_start == unit.get("segment_start")
                               and block.segment_end == unit.get("segment_end"))
                if matches:
                    resume = candidate
                    break
            active = await self.build_index(db, course_id, embed=embed, index_version=resume,
                                            subject_id=subject_id)
            await self.activate(db, active)
        else:
            # 已存在 active：追加该笔记块（如缺失）
            existing = (
                await db.execute(
                    select(func.count(IndexBlock.id)).where(
                        IndexBlock.index_version_id == active.id,
                        IndexBlock.source_note_version_id == note.id,
                    )
                )
            ).scalar_one()
            if existing == 0:
                unit = await self._note_unit_for(db, note, course_id)
                if unit is not None:
                    await self._append_units(db, active, [unit], embed, subject_id=subject_id)

        intent.status = "consumed"
        await db.commit()

    async def _note_unit_for(
        self, db: AsyncSession, note: NoteVersion, course_id: uuid.UUID
    ) -> dict | None:
        content = self._note_text(note)
        if not content.strip():
            return None
        evidence_ids: list[str] = self._note_evidence_ids(note)
        return {
            "source_type": "note",
            "source_version_id": note.id,
            "lecture_id": note.lecture_id,
            "course_id": course_id,
            "block_id": None,
            "page_no": None,
            "heading": None,
            "content": content,
            "segment_start": None,
            "segment_end": None,
            "metadata": {"note_evidence_ids": evidence_ids, "note_status": note.status},
        }

    async def _append_units(
        self, db: AsyncSession, index_version: IndexVersion, units: list[dict], embed,
        subject_id: str | None = None,
    ) -> None:
        next_chunk = (
            await db.execute(
                select(func.coalesce(func.max(IndexBlock.chunk_index), -1)).where(
                    IndexBlock.index_version_id == index_version.id
                )
            )
        ).scalar_one() + 1
        for u in units:
            if settings.CALL_GATEWAY_ENABLED:
                from app.services.ai_call_gateway import CallSpec, call_controlled

                provider_name = getattr(embed, "name", None) or getattr(embed, "model", "embed")
                spec = CallSpec(
                    operation_key=f"index:intent:{index_version.course_id}:{_unit_key(u)}",
                    operation_type="index_embed",
                    operation_family=rate_limit_service.FAMILY_EMBED,
                    provider=provider_name,
                    model=getattr(embed, "model", None),
                    subject_id=subject_id,
                    is_idempotent=False,
                    result_serializer=json_serializer,
                    result_deserializer=json_deserializer,
                )
                outcome = await call_controlled(db, spec, lambda: embed.embed_batch([u["content"]]))
                if outcome.kind == "success":
                    vectors = outcome.result
                    vector = vectors[0] if vectors else None
                elif outcome.kind == "unknown":
                    raise ProviderUnknownError("embedding 结果未知，阻断普通重试")
                elif outcome.error is not None:
                    raise outcome.error
                else:
                    raise ServiceError(422, "embedding_failed",
                                       outcome.error_code or "embedding_failed", retryable=False)
            else:
                vector = (await embed.embed_batch([u["content"]]))[0]
            if vector is None:
                raise ServiceError(422, "embedding_failed", "向量化失败", retryable=False)
            db.add(
                IndexBlock(
                    index_version_id=index_version.id,
                    course_id=u["course_id"],
                    lecture_id=u["lecture_id"],
                    source_type=u["source_type"],
                    source_note_version_id=u["source_version_id"],
                    content=u["content"],
                    embedding=vector,
                    metadata_=u.get("metadata"),
                    chunk_index=next_chunk,
                )
            )
            next_chunk += 1
        index_version.block_count = next_chunk
        await db.commit()

    # ── 状态查询 ──────────────────────────────────────────────────────
    async def get_index_status(
        self, db: AsyncSession, scope: ScopeContext, lecture_id: uuid.UUID
    ) -> dict:
        lecture = await get_accessible_lecture(db, scope, lecture_id)
        course_id = lecture.course_id
        if course_id is None:
            return {"course_id": None, "active_index": None, "status": "none"}
        active = (
            await db.execute(
                select(IndexVersion).where(
                    IndexVersion.course_id == course_id, IndexVersion.status == "active"
                )
            )
        ).scalars().first()
        building = (
            await db.execute(
                select(func.count(IndexVersion.id)).where(
                    IndexVersion.course_id == course_id, IndexVersion.status == "building"
                )
            )
        ).scalar_one()
        status = "none"
        if active is not None:
            status = "building" if building > 0 else "current"
        return {
            "course_id": course_id,
            "active_index": {
                "id": active.id,
                "block_count": active.block_count,
                "source_count": active.source_count,
                "activated_at": active.activated_at,
            } if active else None,
            "status": status,
        }

    async def recover_and_dispatch_index(
        self, db: AsyncSession, embed: EmbeddingProvider | None = None
    ) -> dict:
        """恢复/派发 index 任务与待消费索引意向（至少一次投递）。"""
        stats = {"recovered": 0, "dispatched": 0, "succeeded": 0, "failed": 0, "intents": 0}

        # 回退只读（CA-25）：停写期不领取、不派发、不外发、不发布索引任务/意向。
        from app.services.access import is_read_only

        if is_read_only():
            stats["read_only"] = True
            return stats

        stale_cutoff = _now() - timedelta(seconds=settings.JOB_HEARTBEAT_TIMEOUT_SECONDS)
        stale = (
            await db.execute(
                select(ProcessingJob).where(
                    ProcessingJob.type == "index",
                    ProcessingJob.status == "running",
                    ProcessingJob.heartbeat_at.is_not(None),
                    ProcessingJob.heartbeat_at < stale_cutoff,
                )
            )
        ).scalars().all()
        for job in stale:
            job.status = "queued"
            stats["recovered"] += 1
        if stale:
            await db.commit()

        queued = (
            await db.execute(
                select(ProcessingJob)
                .where(ProcessingJob.type == "index", ProcessingJob.status == "queued")
                .order_by(ProcessingJob.created_at.asc())
            )
        ).scalars().all()
        for job in queued:
            stats["dispatched"] += 1
            await self.run_index_job(db, job.id, embed=embed)
            final = await db.get(ProcessingJob, job.id)
            if final is not None:
                stats["succeeded" if final.status == "succeeded" else "failed"] += 1

        # 消费待索引意向
        intents = (
            await db.execute(
                select(IndexIntent).where(IndexIntent.status == "pending")
                .order_by(IndexIntent.created_at.asc())
            )
        ).scalars().all()
        for intent in intents:
            stats["intents"] += 1
            await self.consume_intent(db, intent, embed=embed)
        return stats


index_service = IndexService()
