"""证据业务逻辑层

读取证据前校验来源归属（课程 owner + 未归档 + 范围约束），
返回原文、定位、来源版本状态与可稳定解析的来源身份（对应 R8）。
录音来源解析片段原文；文档来源返回 block 定位，正文由索引模块（05）提供。

写入侧（对应 R7）通过 ``create_evidence`` / ``link_artifact`` 统一落实：
单一来源、定位字段、合法时间、实际片段存在性、产物类型白名单与归属校验。
"""

import uuid
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.course import Course
from app.models.decision import Decision
from app.models.evidence import EvidenceSpan, ArtifactEvidence, ARTIFACT_TYPES
from app.models.meeting import Meeting
from app.models.note_version import NoteVersion
from app.models.parsed_block import ParsedBlock
from app.models.source_document import SourceDocument, SourceDocumentVersion
from app.models.summary import Summary
from app.models.transcript_version import (
    TranscriptVersion,
    TranscriptSegment,
)
from app.services.scope import ScopeContext


class EvidenceService:
    # ── 读取 ──────────────────────────────────────────────────────────

    async def get_evidence(
        self, db: AsyncSession, scope: ScopeContext, evidence_id: uuid.UUID
    ) -> dict[str, Any]:
        span = (
            await db.execute(select(EvidenceSpan).where(EvidenceSpan.id == evidence_id))
        ).scalar_one_or_none()
        if not span:
            raise HTTPException(status_code=404, detail="证据不存在")

        result: dict[str, Any] = {
            "id": span.id,
            "quote": span.quote,
            "is_valid": span.is_valid,
            "source_type": None,
            "source_version_status": None,
            "source_version_id": None,
            "source_version": None,
            "source_lecture_id": None,
            "source_audio_asset_id": None,
            "source_document_id": None,
            "source_course_id": None,
            "is_current": False,
        }

        if span.transcript_version_id:
            result["source_type"] = "transcript"
            version = await self._check_transcript_owner(
                db, scope, span.transcript_version_id
            )
            result["source_version_status"] = version.status
            result["source_version_id"] = version.id
            result["source_version"] = version.version
            result["source_lecture_id"] = version.lecture_id
            result["source_audio_asset_id"] = version.audio_asset_id
            result["segment_start"] = span.segment_start
            result["segment_end"] = span.segment_end
            result["start_ms"] = span.start_ms
            result["end_ms"] = span.end_ms

            lecture = (
                await db.execute(
                    select(Meeting).where(Meeting.id == version.lecture_id)
                )
            ).scalar_one_or_none()
            if lecture is not None:
                result["source_course_id"] = lecture.course_id
                result["is_current"] = (
                    lecture.active_transcript_version_id == version.id
                )

            # 解析片段原文
            if span.segment_start is not None and span.segment_end is not None:
                segs = (
                    await db.execute(
                        select(TranscriptSegment)
                        .where(
                            TranscriptSegment.version_id == version.id,
                            TranscriptSegment.seq_index >= span.segment_start,
                            TranscriptSegment.seq_index <= span.segment_end,
                        )
                        .order_by(TranscriptSegment.seq_index)
                    )
                ).scalars().all()
                result["content"] = [s.content for s in segs]
            else:
                result["content"] = []
        elif span.document_version_id:
            result["source_type"] = "document"
            version = await self._check_document_owner(
                db, scope, span.document_version_id
            )
            result["source_version_status"] = version.status
            result["source_version_id"] = version.id
            result["source_version"] = version.version
            result["source_document_id"] = version.source_document_id
            result["is_current"] = version.status == "active"
            result["block_id"] = span.block_id
            result["page_no"] = span.page_no

            doc = (
                await db.execute(
                    select(SourceDocument).where(
                        SourceDocument.id == version.source_document_id
                    )
                )
            ).scalar_one_or_none()
            if doc is not None:
                result["source_course_id"] = doc.course_id
                result["source_lecture_id"] = doc.lecture_id

            # 文档来源返回解析块正文（第四阶段：可核对原文，不再留空）
            if span.block_id:
                block = (
                    await db.execute(
                        select(ParsedBlock).where(
                            ParsedBlock.document_version_id == version.id,
                            ParsedBlock.block_id == span.block_id,
                        )
                    )
                ).scalar_one_or_none()
                result["content"] = [block.text] if block is not None else []
                if block is not None:
                    result["page_no"] = block.page_no
            else:
                result["content"] = []
        else:
            raise HTTPException(status_code=404, detail="证据来源无效")

        return result

    async def _check_transcript_owner(
        self, db: AsyncSession, scope: ScopeContext, version_id: uuid.UUID
    ) -> TranscriptVersion:
        row = (
            await db.execute(
                select(TranscriptVersion, Meeting.course_id, Meeting.id)
                .join(Meeting, TranscriptVersion.lecture_id == Meeting.id)
                .join(Course, Meeting.course_id == Course.id)
                .where(
                    TranscriptVersion.id == version_id,
                    Course.owner_id == scope.subject_id,
                    Course.archived_at.is_(None),
                )
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="证据来源不存在")
        version, course_id, lecture_id = row
        if scope.course_id is not None and scope.course_id != course_id:
            raise HTTPException(status_code=404, detail="证据来源不存在")
        if scope.lecture_id is not None and scope.lecture_id != lecture_id:
            raise HTTPException(status_code=404, detail="证据来源不存在")
        return version

    async def _check_document_owner(
        self, db: AsyncSession, scope: ScopeContext, version_id: uuid.UUID
    ) -> SourceDocumentVersion:
        row = (
            await db.execute(
                select(
                    SourceDocumentVersion,
                    SourceDocument.course_id,
                    SourceDocument.lecture_id,
                )
                .join(SourceDocument, SourceDocumentVersion.source_document_id == SourceDocument.id)
                .join(Course, SourceDocument.course_id == Course.id)
                .where(
                    SourceDocumentVersion.id == version_id,
                    Course.owner_id == scope.subject_id,
                    Course.archived_at.is_(None),
                )
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="证据来源不存在")
        version, course_id, lecture_id = row
        if scope.course_id is not None and scope.course_id != course_id:
            raise HTTPException(status_code=404, detail="证据来源不存在")
        if scope.lecture_id is not None and lecture_id is not None and scope.lecture_id != lecture_id:
            raise HTTPException(status_code=404, detail="证据来源不存在")
        return version

    # ── 写入（对应 R7） ──────────────────────────────────────────────

    @staticmethod
    def _validate_time(start_ms: Optional[int], end_ms: Optional[int]) -> None:
        if start_ms is not None and start_ms < 0:
            raise HTTPException(status_code=422, detail="start_ms 不能为负")
        if end_ms is not None and end_ms < 0:
            raise HTTPException(status_code=422, detail="end_ms 不能为负")
        if start_ms is not None and end_ms is not None and start_ms > end_ms:
            raise HTTPException(status_code=422, detail="时间区间倒置")

    async def create_evidence(
        self,
        db: AsyncSession,
        scope: ScopeContext,
        *,
        transcript_version_id: uuid.UUID | None = None,
        segment_start: int | None = None,
        segment_end: int | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        document_version_id: uuid.UUID | None = None,
        block_id: str | None = None,
        page_no: int | None = None,
        quote: str | None = None,
        is_valid: bool = True,
    ) -> EvidenceSpan:
        """统一写入：校验单一来源、定位字段、合法时间与片段存在性"""
        has_transcript = transcript_version_id is not None
        has_document = document_version_id is not None
        if has_transcript == has_document:
            raise HTTPException(
                status_code=422, detail="证据必须且只能指定一种来源（转写或文档）"
            )

        if has_transcript:
            version = await self._check_transcript_owner(db, scope, transcript_version_id)
            if segment_start is None or segment_end is None:
                raise HTTPException(status_code=422, detail="录音来源必须指定片段范围")
            if segment_start < 0 or segment_end < 0:
                raise HTTPException(status_code=422, detail="片段序号不能为负")
            if segment_start > segment_end:
                raise HTTPException(status_code=422, detail="片段范围倒置")

            # 实际片段存在性 + 端点存在性：start/end 两个 seq 都必须在版本内
            segs = (
                await db.execute(
                    select(TranscriptSegment)
                    .where(
                        TranscriptSegment.version_id == version.id,
                        TranscriptSegment.seq_index >= segment_start,
                        TranscriptSegment.seq_index <= segment_end,
                    )
                    .order_by(TranscriptSegment.seq_index)
                )
            ).scalars().all()
            if not segs:
                raise HTTPException(status_code=422, detail="指定片段范围内不存在转写片段")
            seg_seq = {s.seq_index for s in segs}
            if segment_start not in seg_seq or segment_end not in seg_seq:
                raise HTTPException(status_code=422, detail="片段范围端点不存在")

            # 时间来源一致性：从可信源片段（边界）确定允许时间窗，
            # 显式回听时间必须落在源片段完整时间范围内（对应 F6 / G3 / H3）。
            # 只有一侧可信时间时，另一端缺失无法验证显式位置 → 拒绝；
            # 两端全部缺省时仍按约定保留来源的部分/空时间。
            start_seg = next((s for s in segs if s.seq_index == segment_start), None)
            end_seg = next((s for s in segs if s.seq_index == segment_end), None)
            trusted_start = start_seg.start_ms if start_seg is not None else None
            trusted_end = end_seg.end_ms if end_seg is not None else None

            explicit = start_ms is not None or end_ms is not None
            if explicit and (trusted_start is None or trusted_end is None):
                raise HTTPException(
                    status_code=422,
                    detail="源片段时间窗不完整，无法验证显式回听位置",
                )

            if start_ms is not None:
                if start_ms < trusted_start:
                    raise HTTPException(
                        status_code=422, detail="start_ms 早于源片段起始时间"
                    )
                if start_ms > trusted_end:
                    raise HTTPException(
                        status_code=422, detail="start_ms 晚于源片段结束时间"
                    )
            if end_ms is not None:
                if end_ms > trusted_end:
                    raise HTTPException(
                        status_code=422, detail="end_ms 晚于源片段结束时间"
                    )
                if end_ms < trusted_start:
                    raise HTTPException(
                        status_code=422, detail="end_ms 早于源片段起始时间"
                    )

            # 未显式给定的一端从边界片段时间推导
            if start_ms is None:
                start_ms = trusted_start
            if end_ms is None:
                end_ms = trusted_end
            # 推导后仍可能因片段自身时间为 NULL 而为 None，允许
            self._validate_time(start_ms, end_ms)

            # 引用校验：quote 非空，且须能定位到片段正文
            if quote is not None:
                q = quote.strip()
                if not q:
                    raise HTTPException(status_code=422, detail="quote 不能为空")
                joined = "\n".join(s.content or "" for s in segs)
                joined_flat = "".join(s.content or "" for s in segs)
                if q not in joined and q not in joined_flat:
                    raise HTTPException(status_code=422, detail="quote 与片段正文不符")
        else:
            version = await self._check_document_owner(db, scope, document_version_id)
            if not block_id:
                raise HTTPException(status_code=422, detail="文档来源必须指定 block_id")
            # 第四阶段：block_id 必须真实存在于该版本解析块；quote 必须匹配解析块正文，
            # 不再只做非空检查（对应 P04-11）。
            block = (
                await db.execute(
                    select(ParsedBlock).where(
                        ParsedBlock.document_version_id == version.id,
                        ParsedBlock.block_id == block_id,
                    )
                )
            ).scalar_one_or_none()
            if block is None:
                raise HTTPException(status_code=404, detail="block 不存在")
            if quote is not None:
                q = quote.strip()
                if not q:
                    raise HTTPException(status_code=422, detail="quote 不能为空")
                if q not in block.text:
                    raise HTTPException(status_code=422, detail="quote 与解析块正文不符")
            if page_no is None:
                page_no = block.page_no

        span = EvidenceSpan(
            transcript_version_id=transcript_version_id,
            segment_start=segment_start,
            segment_end=segment_end,
            start_ms=start_ms,
            end_ms=end_ms,
            document_version_id=document_version_id,
            block_id=block_id,
            page_no=page_no,
            quote=quote,
            is_valid=is_valid,
        )
        db.add(span)
        await db.flush()
        await db.refresh(span)
        return span

    async def _check_artifact_owned(
        self, db: AsyncSession, scope: ScopeContext, artifact_type: str, artifact_id: uuid.UUID
    ) -> None:
        """校验产物存在且归属当前主体（产物必须能定位到可访问课时）"""
        if artifact_type == "note_version":
            model = NoteVersion
            lecture_attr = model.lecture_id
        elif artifact_type == "decision":
            model = Decision
            lecture_attr = model.meeting_id
        elif artifact_type == "summary":
            model = Summary
            lecture_attr = model.meeting_id
        else:
            # knowledge_point / worked_example / learning_task 由模块 04 建表后启用
            raise HTTPException(status_code=400, detail=f"尚未支持的产物类型: {artifact_type}")

        row = (
            await db.execute(
                select(model, Meeting)
                .join(Meeting, lecture_attr == Meeting.id)
                .join(Course, Meeting.course_id == Course.id)
                .where(
                    model.id == artifact_id,
                    Course.owner_id == scope.subject_id,
                    Course.archived_at.is_(None),
                )
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="产物不存在或无权访问")
        _, meeting = row
        if scope.course_id is not None and scope.course_id != meeting.course_id:
            raise HTTPException(status_code=404, detail="产物不存在或无权访问")
        if scope.lecture_id is not None and scope.lecture_id != meeting.id:
            raise HTTPException(status_code=404, detail="产物不存在或无权访问")

    async def link_artifact(
        self,
        db: AsyncSession,
        scope: ScopeContext,
        artifact_type: str,
        artifact_id: uuid.UUID,
        evidence_id: uuid.UUID,
    ) -> ArtifactEvidence:
        """建立产物与证据关联：白名单 + 产物归属 + 证据存在校验"""
        if artifact_type not in ARTIFACT_TYPES:
            raise HTTPException(status_code=422, detail="非法产物类型")
        await self._check_artifact_owned(db, scope, artifact_type, artifact_id)
        # 证据必须可解析（归属与来源校验）
        await self.get_evidence(db, scope, evidence_id)

        link = ArtifactEvidence(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            evidence_id=evidence_id,
        )
        db.add(link)
        await db.flush()
        await db.refresh(link)
        return link

    async def unlink_artifact(
        self,
        db: AsyncSession,
        artifact_type: str,
        artifact_id: uuid.UUID,
    ) -> None:
        """删除产物时清理其证据关联（删除清理，对应 R7）"""
        await db.execute(
            delete(ArtifactEvidence).where(
                ArtifactEvidence.artifact_type == artifact_type,
                ArtifactEvidence.artifact_id == artifact_id,
            )
        )
        await db.flush()


# 全局实例
evidence_service = EvidenceService()
