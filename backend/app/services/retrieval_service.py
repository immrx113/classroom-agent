"""检索服务（第四阶段：检索闭环）

范围内两路召回（向量 pgvector 余弦 + 词法 ILIKE/tsvector）→ RRF 融合 →
同源去重（按来源身份，不按字符相似度）→ 规则重排 → top_k 截取 →
预算内补取邻接上下文 → 统一证据（来源/版本/定位/可核对原文）。

故障分型：无匹配 → ``no_evidence``；两路均不可用 → ``retrieval_error``；
单路降级 → ``degraded`` 并记录实际分支，降级仍保留主体/课程/课时/版本过滤。
"""

import logging
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.index_version import IndexBlock, IndexVersion
from app.models.meeting import Meeting
from app.models.note_version import NoteVersion
from app.models.parsed_block import ParsedBlock
from app.models.source_document import SourceDocument, SourceDocumentVersion
from app.models.transcript_version import TranscriptSegment, TranscriptVersion
from app.services.access import (
    get_accessible_course,
    get_accessible_lecture,
    index_block_accessible_cond,
    index_version_accessible_cond,
)
from app.services.embedding_service import EmbeddingProvider, get_classroom_embedding
from app.services.errors import ServiceError
from app.services.scope import ScopeContext

logger = logging.getLogger(__name__)

# 测试注入：分别令向量 / 词法 / embedding 分支失败（P04-14）。生产恒 False。
EMBED_FAIL = False
VECTOR_FAIL = False
LEXICAL_FAIL = False
# 测试注入：可控重排评分（P04-08），覆盖默认规则重排。生产恒 None。
_rerank_fn = None


def _default_embed() -> EmbeddingProvider:
    return get_classroom_embedding()


class RetrievalService:
    # ── 范围归一化 ────────────────────────────────────────────────────
    async def _normalize_scope(self, db: AsyncSession, scope: ScopeContext) -> ScopeContext:
        if scope.lecture_id is not None:
            lecture = await get_accessible_lecture(db, scope, scope.lecture_id)
            return ScopeContext(
                subject_id=scope.subject_id,
                course_id=lecture.course_id,
                lecture_id=scope.lecture_id,
            )
        if scope.course_id is not None:
            await get_accessible_course(db, scope, scope.course_id)
            return scope
        return scope

    async def _active_index_ids(self, db: AsyncSession, scope: ScopeContext) -> list[uuid.UUID]:
        cond = index_version_accessible_cond(scope) & (IndexVersion.status == "active")
        rows = (
            await db.execute(select(IndexVersion.id).where(cond))
        ).scalars().all()
        return list(rows)

    # ── 两路召回 ──────────────────────────────────────────────────────
    async def _embed_query(
        self, embed: EmbeddingProvider, query: str
    ) -> Optional[list[float]]:
        if EMBED_FAIL:
            return None
        try:
            vec = await embed.embed_text(query)
        except Exception:
            logger.warning("查询向量化不可用，保留范围内词法召回")
            return None
        if not vec:
            return None
        return vec

    async def _vector_recall(
        self, db: AsyncSession, scope: ScopeContext, version_ids: list, query_vec, limit: int
    ) -> list[dict]:
        if VECTOR_FAIL:
            raise RuntimeError("vector branch injected failure")
        stmt = (
            select(
                IndexBlock,
                IndexBlock.embedding.cosine_distance(query_vec).label("distance"),
            )
            .where(
                IndexBlock.index_version_id.in_(version_ids),
                index_block_accessible_cond(scope),
                IndexBlock.embedding.is_not(None),
                # 相关性阈值：过滤无共享内容（余弦相似度≈0）的弱匹配，否则
                # 确定性/真实 embedding 都会对任意查询返回非零相似度的全量候选，
                # 「无匹配 → no_evidence」将永远不可达（P04-14）。
                IndexBlock.embedding.cosine_distance(query_vec)
                <= (1.0 - settings.INDEX_RETRIEVAL_RELEVANCE_THRESHOLD),
            )
            .order_by("distance")
            .limit(limit)
        )
        rows = (await db.execute(stmt)).all()
        return [
            {"block": b, "vector_score": max(0.0, 1.0 - d), "lexical_score": None}
            for b, d in rows
        ]

    async def _lexical_recall(
        self, db: AsyncSession, scope: ScopeContext, version_ids: list, query: str, limit: int
    ) -> list[dict]:
        if LEXICAL_FAIL:
            raise RuntimeError("lexical branch injected failure")
        safe = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{safe}%"
        stmt = (
            select(IndexBlock)
            .where(
                IndexBlock.index_version_id.in_(version_ids),
                index_block_accessible_cond(scope),
                IndexBlock.content.ilike(pattern, escape="\\"),
            )
            .limit(limit)
        )
        blocks = (await db.execute(stmt)).scalars().all()
        return [
            {"block": b, "vector_score": None, "lexical_score": 1.0} for b in blocks
        ]

    # ── 融合 / 去重 / 重排 ────────────────────────────────────────────
    def _source_key(self, b: IndexBlock) -> tuple:
        if b.source_type == "transcript":
            return (b.source_type, str(b.source_transcript_version_id), b.segment_start, b.segment_end)
        if b.source_type == "document":
            return (b.source_type, str(b.source_document_version_id), b.block_id)
        return (b.source_type, str(b.source_note_version_id), b.block_id)

    def _rrf_fusion(self, vector: list[dict], lexical: list[dict], k: int) -> list[dict]:
        scores: dict[tuple, float] = {}
        blocks: dict[tuple, IndexBlock] = {}
        for rank, item in enumerate(vector):
            key = self._source_key(item["block"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            blocks[key] = item["block"]
        for rank, item in enumerate(lexical):
            key = self._source_key(item["block"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in blocks:
                blocks[key] = item["block"]
        order = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        return [{"block": blocks[k], "rrf_score": scores[k]} for k in order]

    def _rule_rerank(self, query: str, fused: list[dict]) -> list[dict]:
        q_grams = self._grams(query)
        for item in fused:
            c_grams = self._grams(item["block"].content)
            overlap = len(q_grams & c_grams)
            item["rerank_score"] = item["rrf_score"] + overlap
        fused.sort(key=lambda x: x["rerank_score"], reverse=True)
        return fused

    @staticmethod
    def _grams(text: str) -> set[str]:
        t = text.strip().lower()
        return {t[i:i + 2] for i in range(len(t) - 1)} | {t[i] for i in range(len(t))}

    # ── 来源解析 ──────────────────────────────────────────────────────
    async def resolve_source(
        self,
        db: AsyncSession,
        scope: ScopeContext,
        *,
        source_type: str,
        source_version_id: uuid.UUID,
        block_id: Optional[str] = None,
        segment_start: Optional[int] = None,
        segment_end: Optional[int] = None,
        quote: Optional[str] = None,
    ) -> dict:
        """解析来源为实际正文 + 定位；越权 404，非法 block/片段或 quote 不匹配 422/404。"""
        scope = await self._normalize_scope(db, scope)
        if source_type == "document":
            return await self._resolve_document(
                db, scope, source_version_id, block_id, quote
            )
        if source_type == "transcript":
            return await self._resolve_transcript(
                db, scope, source_version_id, segment_start, segment_end
            )
        if source_type == "note":
            return await self._resolve_note(db, scope, source_version_id)
        raise ServiceError(422, "invalid_input", "未知来源类型", retryable=False)

    async def _resolve_document(
        self, db, scope, version_id, block_id, quote
    ) -> dict:
        ver = (
            await db.execute(
                select(SourceDocumentVersion).where(SourceDocumentVersion.id == version_id)
            )
        ).scalar_one_or_none()
        if ver is None:
            raise ServiceError(404, "not_found", "文档版本不存在", retryable=False)
        doc = await db.get(SourceDocument, ver.source_document_id)
        if doc is None:
            raise ServiceError(404, "not_found", "文档不存在", retryable=False)
        # 归属校验：课程 owner + 未归档 + 范围
        await get_accessible_course(db, scope, doc.course_id)
        if scope.lecture_id is not None and doc.lecture_id not in (None, scope.lecture_id):
            raise ServiceError(404, "not_found", "文档不存在", retryable=False)

        if not block_id:
            raise ServiceError(422, "invalid_input", "文档来源必须指定 block_id", retryable=False)
        block = (
            await db.execute(
                select(ParsedBlock).where(
                    ParsedBlock.document_version_id == version_id,
                    ParsedBlock.block_id == block_id,
                )
            )
        ).scalar_one_or_none()
        if block is None:
            raise ServiceError(404, "not_found", "block 不存在", retryable=False)

        # quote 必须匹配实际 ParsedBlock（P04-11：不再只做非空检查）
        if quote is not None:
            q = quote.strip()
            if q and q not in block.text:
                raise ServiceError(422, "invalid_input", "quote 与解析块正文不符", retryable=False)

        latest_id = (await db.execute(select(SourceDocumentVersion.id).where(
            SourceDocumentVersion.source_document_id == doc.id,
            SourceDocumentVersion.status == "active",
        ).order_by(SourceDocumentVersion.version.desc()).limit(1))).scalar_one_or_none()

        return {
            "source_type": "document",
            "source_version_id": version_id,
            "source_version": ver.version,
            "is_current": latest_id == version_id,
            "source_document_id": doc.id,
            "source_course_id": doc.course_id,
            "source_lecture_id": doc.lecture_id,
            "block_id": block.block_id,
            "page_no": block.page_no,
            "heading": block.heading,
            "paragraph_index": block.paragraph_index,
            "table_index": block.table_index,
            "content": [block.text],
        }

    async def _resolve_transcript(
        self, db, scope, version_id, segment_start, segment_end
    ) -> dict:
        ver = await db.get(TranscriptVersion, version_id)
        if ver is None:
            raise ServiceError(404, "not_found", "转写版本不存在", retryable=False)
        await get_accessible_lecture(db, scope, ver.lecture_id)
        if segment_start is None or segment_end is None:
            raise ServiceError(422, "invalid_input", "转写来源必须指定片段范围", retryable=False)
        segs = (
            await db.execute(
                select(TranscriptSegment)
                .where(
                    TranscriptSegment.version_id == version_id,
                    TranscriptSegment.seq_index >= segment_start,
                    TranscriptSegment.seq_index <= segment_end,
                )
                .order_by(TranscriptSegment.seq_index)
            )
        ).scalars().all()
        if not segs:
            raise ServiceError(404, "not_found", "片段范围不存在", retryable=False)
        lecture = await db.get(Meeting, ver.lecture_id)
        return {
            "source_type": "transcript",
            "source_version_id": version_id,
            "source_version": ver.version,
            "source_lecture_id": ver.lecture_id,
            "source_course_id": lecture.course_id if lecture else None,
            "source_audio_asset_id": ver.audio_asset_id,
            "is_current": bool(lecture and lecture.active_transcript_version_id == version_id),
            "segment_start": segs[0].seq_index,
            "segment_end": segs[-1].seq_index,
            "start_ms": segs[0].start_ms,
            "end_ms": segs[-1].end_ms,
            "content": [s.content for s in segs],
        }

    async def _resolve_note(self, db, scope, version_id) -> dict:
        note = await db.get(NoteVersion, version_id)
        if note is None:
            raise ServiceError(404, "not_found", "笔记不存在", retryable=False)
        lecture = await get_accessible_lecture(db, scope, note.lecture_id)
        return {
            "source_type": "note",
            "source_version_id": version_id,
            "source_lecture_id": note.lecture_id,
            "source_course_id": lecture.course_id,
            "is_current": lecture.active_note_version_id == version_id,
            "note_status": note.status,
            "content": [self._note_text(note)],
        }

    @staticmethod
    def _note_text(note) -> str:
        # 失效引用被降「待确认（pending）」的条目须从正式知识中排除（A4/R4）：仅返回 confirmed。
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

    # ── 主检索 ────────────────────────────────────────────────────────
    async def search(
        self,
        db: AsyncSession,
        scope: ScopeContext,
        query: str,
        top_k: Optional[int] = None,
        embed: Optional[EmbeddingProvider] = None,
    ) -> dict:
        top_k = top_k or settings.INDEX_TOP_K
        scope = await self._normalize_scope(db, scope)
        embed = embed or _default_embed()

        version_ids = await self._active_index_ids(db, scope)
        trace = {"vector": None, "lexical": None, "branches": []}
        if not version_ids:
            return self._response(query, "no_evidence", results=[], trace=trace,
                                  index_status="none", index_version_id=None)

        compatible_ids = list((await db.execute(
            select(IndexVersion.id).where(
                IndexVersion.id.in_(version_ids),
                IndexVersion.config["embedding_model"].astext == embed.model,
                IndexVersion.config["embedding_dimensions"].astext == str(embed.dimensions),
            )
        )).scalars().all())
        query_vec = await self._embed_query(embed, query) if compatible_ids else None

        vector: list[dict] = []
        lexical: list[dict] = []
        vector_error = False
        lexical_error = False

        if query_vec is not None:
            try:
                vector = await self._vector_recall(
                    db, scope, compatible_ids, query_vec, settings.INDEX_CANDIDATE_POOL
                )
                trace["branches"].append("vector")
            except Exception as e:  # noqa: BLE001
                logger.warning("向量召回失败: %s", e)
                vector_error = True
        else:
            vector_error = True

        try:
            lexical = await self._lexical_recall(
                db, scope, version_ids, query, settings.INDEX_CANDIDATE_POOL
            )
            trace["branches"].append("lexical")
        except Exception as e:  # noqa: BLE001
            logger.warning("词法召回失败: %s", e)
            lexical_error = True

        trace["vector"] = len(vector)
        trace["lexical"] = len(lexical)
        trace["vector_error"] = vector_error
        trace["lexical_error"] = lexical_error

        # 两路均不可用 → retrieval_error（P04-14）
        if vector_error and lexical_error:
            return self._response(query, "retrieval_error", results=[], trace=trace,
                                  index_status="current", index_version_id=version_ids[0])

        fused = self._rrf_fusion(vector, lexical, settings.INDEX_RRF_K)
        trace["fused"] = len(fused)

        # 同源去重（按来源身份，不按字符相似度）
        deduped: list[dict] = []
        seen: set[tuple] = set()
        for item in fused:
            key = self._source_key(item["block"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        trace["deduped"] = len(deduped)

        # 重排（默认规则重排，可注入可控评分）
        if _rerank_fn is not None:
            deduped = _rerank_fn(query, deduped)
        else:
            deduped = self._rule_rerank(query, deduped)

        # top_k 截取（融合/去重/重排后）
        selected = deduped[:top_k]
        trace["selected"] = len(selected)

        # 整次响应预算：所有命中正文与邻接正文合计受 INDEX_CONTEXT_BUDGET 约束，
        # 而非每条命中各自重新占用完整预算（否则多命中会成倍超额）。
        # 先解析全部命中正文并累计，再以扣除全部命中正文后的剩余预算统一补取邻接，
        # 避免前面的命中按其自身正文单独扣减预算，把后续命中的正文挤占掉。
        resolved_items: list[tuple[IndexBlock, dict, dict]] = []
        for item in selected:
            b: IndexBlock = item["block"]
            resolved = await self._resolve_item(db, scope, b)
            if resolved is None:
                continue
            resolved_items.append((b, resolved, item))

        total_hit = sum(sum(len(c) for c in r["content"]) for _, r, _ in resolved_items)
        adjacent_budget = max(0, settings.INDEX_CONTEXT_BUDGET - total_hit)

        results: list[dict] = []
        remaining = adjacent_budget
        for b, resolved, item in resolved_items:
            adjacent = await self._adjacent_context(db, scope, b, remaining)
            remaining = max(0, remaining - sum(len(a) for a in adjacent))
            results.append(self._assemble_result(b, resolved, item, adjacent))

        status = "no_evidence" if not results else ("degraded" if (vector_error or lexical_error) else "ok")
        single_version = str(version_ids[0]) if len(version_ids) == 1 else None
        return self._response(
            query, status, results=results, trace=trace,
            index_status="current",
            index_version_id=single_version,
        )

    @staticmethod
    def _response(query, status, *, results, trace, index_status, index_version_id) -> dict:
        return {
            "query": query,
            "status": status,
            "index_status": index_status,
            "index_version_id": str(index_version_id) if index_version_id else None,
            "results": results,
            "total": len(results),
            "trace": trace,
        }

    async def _resolve_item(self, db, scope, b: IndexBlock) -> Optional[dict]:
        """解析命中来源为统一证据（不含邻接），邻接由调用方在整次预算核算后补取。"""
        try:
            if b.source_type == "transcript":
                return await self._resolve_transcript(
                    db, scope, b.source_transcript_version_id, b.segment_start, b.segment_end
                )
            if b.source_type == "document":
                return await self._resolve_document(
                    db, scope, b.source_document_version_id, b.block_id, quote=b.content
                )
            if b.source_type == "note":
                return await self._resolve_note(db, scope, b.source_note_version_id)
            return None
        except (ServiceError, HTTPException):
            return None

    def _assemble_result(
        self, b: IndexBlock, resolved: dict, item: dict, adjacent: list[str]
    ) -> dict:
        return {
            "source_type": resolved["source_type"],
            "source_version_id": str(resolved["source_version_id"]),
            "source_version": resolved.get("source_version"),
            "source_lecture_id": str(resolved["source_lecture_id"]) if resolved.get("source_lecture_id") else None,
            "source_course_id": str(resolved["source_course_id"]) if resolved.get("source_course_id") else None,
            "source_document_id": str(resolved["source_document_id"]) if resolved.get("source_document_id") else None,
            "source_audio_asset_id": str(resolved["source_audio_asset_id"]) if resolved.get("source_audio_asset_id") else None,
            "is_current": bool(resolved.get("is_current")),
            "block_id": resolved.get("block_id"),
            "page_no": resolved.get("page_no"),
            "heading": resolved.get("heading"),
            "paragraph_index": resolved.get("paragraph_index"),
            "table_index": resolved.get("table_index"),
            "segment_start": resolved.get("segment_start"),
            "segment_end": resolved.get("segment_end"),
            "start_ms": resolved.get("start_ms"),
            "end_ms": resolved.get("end_ms"),
            "content": resolved["content"],
            "quote": b.content,
            "score": round(float(item["rrf_score"]), 6),
            "rerank_score": round(float(item.get("rerank_score", 0.0)), 6),
            "adjacent": adjacent,
            "note_evidence_ids": (b.metadata_ or {}).get("note_evidence_ids") or [],
        }

    async def _adjacent_context(
        self, db, scope, b: IndexBlock, budget: int
    ) -> list[str]:
        """补取邻接上下文：同一授权来源版本内相邻块/片段，受剩余预算与单来源限额约束。

        ``budget`` 为扣除全部命中正文后、整次响应剩余的邻接字符预算（调用方统一核算，
        并随每条结果累计扣减）。
        """
        limit = settings.INDEX_SINGLE_SOURCE_LIMIT
        try:
            if b.source_type == "transcript":
                segs = (
                    await db.execute(
                        select(TranscriptSegment)
                        .where(TranscriptSegment.version_id == b.source_transcript_version_id)
                        .order_by(TranscriptSegment.seq_index)
                    )
                ).scalars().all()
                out: list[str] = []
                used = 0
                # 相邻片段：紧邻命中范围之前与之后，由近及远，受单来源限额与预算约束。
                before = [s for s in segs if s.seq_index < b.segment_start][::-1]
                after = [s for s in segs if s.seq_index > b.segment_end]
                for s in before + after:
                    if len(out) >= limit:
                        break
                    if used + len(s.content or "") > budget:
                        break
                    out.append(s.content or "")
                    used += len(s.content or "")
                return out
            if b.source_type == "document":
                blocks = (
                    await db.execute(
                        select(ParsedBlock)
                        .where(ParsedBlock.document_version_id == b.source_document_version_id)
                        .order_by(ParsedBlock.seq)
                    )
                ).scalars().all()
                hit_seq = next((x.seq for x in blocks if x.block_id == b.block_id), None)
                if hit_seq is None:
                    return []
                out = []
                used = 0
                for x in blocks:
                    if x.block_id != b.block_id and abs(x.seq - hit_seq) == 1:
                        if len(out) >= limit:
                            break
                        if used + len(x.text) > budget:
                            break
                        out.append(x.text)
                        used += len(x.text)
                return out
            return []
        except Exception:  # noqa: BLE001
            return []


retrieval_service = RetrievalService()
