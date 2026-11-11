"""课件文档服务（第四阶段）

上传 → 解析（真实定位块）→ 文档/版本/块持久化；列表 / 按身份删除 / 读取解析块。
同名文件以稳定 document_id 区分，不按 title 合并或扩散删除。
空文本 / 不支持格式持久化为明确解析状态，不伪装“解析/索引成功”。
"""

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.course import Course
from app.models.index_version import IndexBlock
from app.models.meeting import Meeting
from app.models.parsed_block import ParsedBlock
from app.models.source_document import SourceDocument, SourceDocumentVersion
from app.services.access import (
    document_accessible_cond,
    get_accessible_course,
    get_accessible_document,
)
from app.services.document_parser import ParseError, document_parser
from app.services.errors import ServiceError
from app.services.scope import ScopeContext

logger = logging.getLogger(__name__)

PARSER_VERSION = "phase4-parser-v1"
CHUNKER_VERSION = "phase4-chunker-v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ext(filename: str) -> str:
    return filename.lower().split(".")[-1] if "." in filename else ""


class DocumentService:
    async def create_document(
        self,
        db: AsyncSession,
        scope: ScopeContext,
        *,
        course_id: uuid.UUID,
        lecture_id: uuid.UUID | None,
        filename: str,
        content: bytes,
        mime: str | None,
    ) -> tuple[SourceDocument, SourceDocumentVersion]:
        """上传并解析文档，返回（文档，版本）。解析失败版本也持久化（可查询失败原因）。"""
        await get_accessible_course(db, scope, course_id)
        if lecture_id is not None:
            meeting = await db.get(Meeting, lecture_id)
            if meeting is None or meeting.course_id != course_id:
                raise ServiceError(404, "invalid_input", "课时不存在或不属于该课程", retryable=False)

        title = Path(filename).stem or filename
        raw_hash = _sha256(content)

        doc = SourceDocument(
            course_id=course_id,
            lecture_id=lecture_id,
            title=title,
            filename=filename,
            storage_key=None,
            mime=mime,
            content_hash=raw_hash,
        )
        db.add(doc)
        await db.flush()

        version = SourceDocumentVersion(
            source_document_id=doc.id,
            version=1,
            status="building",
            parse_status="parsed",
            config={
                "parser_version": PARSER_VERSION,
                "chunker_version": CHUNKER_VERSION,
                "chunk_size": settings.INDEX_CHUNK_SIZE,
                "mime": mime,
            },
            block_count=0,
        )
        db.add(version)
        await db.flush()

        # 持久化原始字节（解析失败也可追溯），路径不对外暴露。
        upload_dir = Path(settings.UPLOAD_DIR) / "documents"
        upload_dir.mkdir(parents=True, exist_ok=True)
        ext = _ext(filename)
        rel_key = f"documents/{doc.id}.{ext}" if ext else f"documents/{doc.id}"
        (upload_dir / f"{doc.id}.{ext or 'bin'}").write_bytes(content)
        doc.storage_key = rel_key

        try:
            blocks = document_parser.parse(str(upload_dir / f"{doc.id}.{ext or 'bin'}"), filename)
        except ParseError as e:
            version.parse_status = e.code
            version.error_code = e.code
            version.status = "failed"
            version.block_count = 0
            await db.flush()
            await db.refresh(doc)
            await db.refresh(version)
            return doc, version

        parsed_text = "\n".join(b.text for b in blocks)
        version.content_hash = _sha256(parsed_text.encode("utf-8"))
        version.block_count = len(blocks)
        version.status = "active"
        version.parse_status = "parsed"

        for seq, b in enumerate(blocks):
            db.add(
                ParsedBlock(
                    document_version_id=version.id,
                    block_id=b.block_id,
                    seq=seq,
                    kind=b.kind,
                    text=b.text,
                    heading=b.heading,
                    page_no=b.page_no,
                    paragraph_index=b.paragraph_index,
                    table_index=b.table_index,
                )
            )
        await db.flush()
        await db.refresh(doc)
        await db.refresh(version)
        return doc, version

    async def add_version(
        self,
        db: AsyncSession,
        scope: ScopeContext,
        document_id: uuid.UUID,
        *,
        filename: str,
        content: bytes,
        mime: str | None,
    ) -> SourceDocumentVersion:
        """对既有文档建立后续解析/索引版本（保留文件身份）。"""
        doc = await get_accessible_document(db, scope, document_id)
        raw_hash = _sha256(content)

        # 同一内容不重复建版本（幂等）
        existing = (
            await db.execute(
                select(SourceDocumentVersion).where(
                    SourceDocumentVersion.source_document_id == document_id,
                    SourceDocumentVersion.content_hash == raw_hash,
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing

        versions = await self._versions(db, doc.id)
        next_version = max((v.version for v in versions), default=0) + 1

        version = SourceDocumentVersion(
            source_document_id=doc.id,
            version=next_version,
            status="building",
            parse_status="parsed",
            config={
                "parser_version": PARSER_VERSION,
                "chunker_version": CHUNKER_VERSION,
                "chunk_size": settings.INDEX_CHUNK_SIZE,
                "mime": mime,
            },
            block_count=0,
        )
        db.add(version)
        await db.flush()

        upload_dir = Path(settings.UPLOAD_DIR) / "documents"
        upload_dir.mkdir(parents=True, exist_ok=True)
        ext = _ext(filename)
        (upload_dir / f"{doc.id}-{version.version}.{ext or 'bin'}").write_bytes(content)

        try:
            blocks = document_parser.parse(str(upload_dir / f"{doc.id}-{version.version}.{ext or 'bin'}"), filename)
        except ParseError as e:
            version.parse_status = e.code
            version.error_code = e.code
            version.status = "failed"
            version.block_count = 0
            await db.flush()
            await db.refresh(version)
            return version

        parsed_text = "\n".join(b.text for b in blocks)
        version.content_hash = _sha256(parsed_text.encode("utf-8"))
        version.block_count = len(blocks)
        version.status = "active"

        for seq, b in enumerate(blocks):
            db.add(
                ParsedBlock(
                    document_version_id=version.id,
                    block_id=b.block_id,
                    seq=seq,
                    kind=b.kind,
                    text=b.text,
                    heading=b.heading,
                    page_no=b.page_no,
                    paragraph_index=b.paragraph_index,
                    table_index=b.table_index,
                )
            )
        await db.flush()
        await db.refresh(version)
        return version

    async def _versions(self, db: AsyncSession, document_id: uuid.UUID) -> list[SourceDocumentVersion]:
        rows = (
            await db.execute(
                select(SourceDocumentVersion).where(
                    SourceDocumentVersion.source_document_id == document_id
                )
            )
        ).scalars().all()
        return list(rows)

    async def list_documents(
        self, db: AsyncSession, scope: ScopeContext, course_id: uuid.UUID
    ) -> list[dict]:
        """按课程列出文档（含最新版本摘要），身份独立，不按 title 合并。"""
        await get_accessible_course(db, scope, course_id)
        docs = (
            await db.execute(
                select(SourceDocument).where(
                    SourceDocument.course_id == course_id,
                    document_accessible_cond(scope),
                ).order_by(SourceDocument.created_at.asc())
            )
        ).scalars().all()
        out: list[dict] = []
        for doc in docs:
            versions = await self._versions(db, doc.id)
            latest = versions[-1] if versions else None
            out.append(
                {
                    "id": doc.id,
                    "course_id": doc.course_id,
                    "lecture_id": doc.lecture_id,
                    "title": doc.title,
                    "filename": doc.filename,
                    "mime": doc.mime,
                    "content_hash": doc.content_hash,
                    "created_at": doc.created_at,
                    "latest_version": self._version_summary(latest) if latest else None,
                }
            )
        return out

    @staticmethod
    def _version_summary(v: SourceDocumentVersion) -> dict:
        return {
            "id": v.id,
            "version": v.version,
            "status": v.status,
            "parse_status": v.parse_status,
            "error_code": v.error_code,
            "block_count": v.block_count,
        }

    async def get_document(
        self, db: AsyncSession, scope: ScopeContext, document_id: uuid.UUID
    ) -> dict:
        doc = await get_accessible_document(db, scope, document_id)
        versions = await self._versions(db, doc.id)
        return {
            "id": doc.id,
            "course_id": doc.course_id,
            "lecture_id": doc.lecture_id,
            "title": doc.title,
            "filename": doc.filename,
            "mime": doc.mime,
            "content_hash": doc.content_hash,
            "created_at": doc.created_at,
            "versions": [self._version_summary(v) for v in versions],
        }

    async def get_blocks(
        self, db: AsyncSession, scope: ScopeContext, document_id: uuid.UUID
    ) -> list[dict]:
        """返回文档（最新有效版本）的解析块与真实定位。"""
        doc = await get_accessible_document(db, scope, document_id)
        versions = await self._versions(db, doc.id)
        version = next((v for v in reversed(versions) if v.status != "failed"), None)
        if version is None:
            return []
        blocks = (
            await db.execute(
                select(ParsedBlock)
                .where(ParsedBlock.document_version_id == version.id)
                .order_by(ParsedBlock.seq)
            )
        ).scalars().all()
        return [
            {
                "block_id": b.block_id,
                "kind": b.kind,
                "text": b.text,
                "heading": b.heading,
                "page_no": b.page_no,
                "paragraph_index": b.paragraph_index,
                "table_index": b.table_index,
            }
            for b in blocks
        ]

    async def delete_document(
        self, db: AsyncSession, scope: ScopeContext, document_id: uuid.UUID
    ) -> bool:
        """按文档身份删除：文档及其版本/块移除，索引块同步清除（立即停止召回）。

        删除集合不按 title 扩散；其他课程同名资料保留。
        """
        doc = await get_accessible_document(db, scope, document_id)
        version_ids = [v.id for v in await self._versions(db, doc.id)]

        if version_ids:
            await db.execute(
                delete(IndexBlock).where(
                    IndexBlock.source_type == "document",
                    IndexBlock.source_document_version_id.in_(version_ids),
                )
            )
        await db.flush()

        # 版本级联删除解析块；再删除文档。
        await db.execute(
            delete(SourceDocumentVersion).where(
                SourceDocumentVersion.source_document_id == document_id
            )
        )
        await db.delete(doc)
        await db.flush()
        return True


document_service = DocumentService()
