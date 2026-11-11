"""compensate_classroom_phase1_round2

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-10-27

阶段一第二轮补偿（对应复验 G1 / G2）。

背景：d5e6f7a8b9c0 补偿迁移在评审后被就地编辑过两次——
- 约束放宽：单来源约束由严格形式改成「隔离证据可无来源」的放宽形式；
- 回填实现：由「已有版本一律跳过」改成「已有版本按可确定字段修复」。

已经应用过「旧 d5」的库，其 alembic_version 已指向 d5，不会重跑被编辑的内容，
因此这些库仍停留在旧状态。本 revision 通过显式核对并修复这两类差异，
使「已应用旧 d5」的库与 fresh 路径收敛到同一最终状态：

1. G1  单来源约束：核对 ``ck_evidence_span_single_source`` 实际定义，
   若仍为严格形式 ``(tx IS NOT NULL) != (doc IS NOT NULL)``（旧 d5），
   则替换为放宽形式 ``(is_valid = FALSE) OR ...``（与 fresh 一致）。
2. G2  旧回填修复：重跑带修复路径的增量回填，把原版 e5 错误回填遗留的
   演示标记 / 版本状态 / 活动指针 / 旧片段映射修正；未知或冲突记录隔离并报告。
"""

import logging

from alembic import op
import sqlalchemy as sa

from app.config import settings
from app.db.backfill import backfill_legacy_course, backfill_transcript_versions

revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic")

_RELAXED_SINGLE_SOURCE = (
    "(is_valid = FALSE) OR "
    "((transcript_version_id IS NOT NULL) != (document_version_id IS NOT NULL))"
)


def _current_constraint_def(bind) -> str | None:
    """返回单来源约束当前定义文本；不存在则返回 None。"""
    row = bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE c.conname = 'ck_evidence_span_single_source' "
            "AND t.relname = 'evidence_spans'"
        )
    ).fetchone()
    return row[0] if row else None


def upgrade() -> None:
    bind = op.get_bind()

    # G1：核对并修复单来源约束定义（旧 d5 仍为严格形式）
    current = _current_constraint_def(bind)
    if current is not None and "is_valid" not in current:
        op.drop_constraint("ck_evidence_span_single_source", "evidence_spans", type_="check")
        op.create_check_constraint(
            "ck_evidence_span_single_source",
            "evidence_spans",
            _RELAXED_SINGLE_SOURCE,
        )
        log.info("G1 单来源约束已由严格形式放宽为隔离兼容形式")
    else:
        log.info("G1 单来源约束已为放宽形式，无需修改")

    # G2：重跑带修复路径的增量回填
    backfill_legacy_course(bind, settings.LOCAL_SUBJECT_ID)
    report = backfill_transcript_versions(bind, settings.LOCAL_SUBJECT_ID)
    log.info("classroom phase1 round2 backfill report: %s", report)


def downgrade() -> None:
    # 有损降级：仅恢复严格单来源约束；回填修复不回退。
    op.drop_constraint("ck_evidence_span_single_source", "evidence_spans", type_="check")
    op.create_check_constraint(
        "ck_evidence_span_single_source",
        "evidence_spans",
        "(transcript_version_id IS NOT NULL) != (document_version_id IS NOT NULL)",
    )
