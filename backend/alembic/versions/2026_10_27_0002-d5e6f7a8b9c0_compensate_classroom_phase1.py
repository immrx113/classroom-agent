"""compensate_classroom_phase1_fixes

Revision ID: d5e6f7a8b9c0
Revises: e5f6a7b8c9d0
Create Date: 2026-10-27

阶段一补偿修复。已应用 e5f6a7b8c9d0 的库，以及未升级的新库，都会走到本 revision，
从而把两条路径收敛到同一最终状态。

修正内容（对应验收 R4 / R5 / R7，复验 F2 / F3 / F4）：

1. audio_assets.storage_key 改可空：缺失音频用 NULL 显式表示。
2. transcript_segments 增加 source_transcript_id：保留旧 transcripts.id 稳定映射。
3. **先盘点修复既有数据，再启用约束**（F3）：
   - 转写来源证据缺/倒置片段范围 → 隔离（is_valid=False + 解除来源），保留异常记录，不删除。
   - 文档来源证据缺 block_id → 隔离。
   - 证据/片段负时间、倒置时间 → 清空为 NULL。
   - 产物关联白名单外类型 → 删除（仅链接行，非证据）。
4. **增量幂等回填，保留既有合法产物**（F2）：不再无条件 DELETE
   audio_assets / transcript_versions / transcript_segments；backfill 仅补建
   「尚无版本」的课时，已存在的版本/证据/笔记来源不动。
"""

import logging

from alembic import op
import sqlalchemy as sa

from app.config import settings
from app.db.backfill import backfill_legacy_course, backfill_transcript_versions

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic")

_ARTIFACT_TYPES = (
    "note_version",
    "knowledge_point",
    "worked_example",
    "learning_task",
    "decision",
    "summary",
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. 缺失音频的显式状态：storage_key 可空
    op.alter_column(
        "audio_assets",
        "storage_key",
        existing_type=sa.String(length=500),
        nullable=True,
    )

    # 2. 旧片段稳定映射
    op.add_column(
        "transcript_segments",
        sa.Column("source_transcript_id", sa.UUID(), nullable=True),
    )

    # 2b. 隔离需「解除来源」；先放宽单来源约束到「无效证据可无来源」，
    #     否则置空 transcript_version_id / document_version_id 会违反 e5 的严格约束
    op.drop_constraint("ck_evidence_span_single_source", "evidence_spans", type_="check")

    # 3. 约束启用前盘点修复既有数据（F3）：不删除证据，无法推断定位的隔离并保留异常记录
    # 3a. 转写来源证据缺/倒置片段范围 → 隔离
    bind.execute(
        sa.text(
            "UPDATE evidence_spans SET is_valid = FALSE, transcript_version_id = NULL "
            "WHERE transcript_version_id IS NOT NULL AND "
            "(segment_start IS NULL OR segment_end IS NULL OR segment_start > segment_end)"
        )
    )
    # 3b. 文档来源证据缺 block_id → 隔离
    bind.execute(
        sa.text(
            "UPDATE evidence_spans SET is_valid = FALSE, document_version_id = NULL "
            "WHERE document_version_id IS NOT NULL AND block_id IS NULL"
        )
    )
    # 3c. 证据时间：负值清空；倒置双双清空
    bind.execute(
        sa.text("UPDATE evidence_spans SET start_ms = NULL WHERE start_ms < 0")
    )
    bind.execute(
        sa.text("UPDATE evidence_spans SET end_ms = NULL WHERE end_ms < 0")
    )
    bind.execute(
        sa.text(
            "UPDATE evidence_spans SET start_ms = NULL, end_ms = NULL "
            "WHERE start_ms IS NOT NULL AND end_ms IS NOT NULL AND start_ms > end_ms"
        )
    )
    # 3d. 转写片段时间：负值清空；倒置双双清空（旧回填或合法产品可能含倒置）
    bind.execute(
        sa.text("UPDATE transcript_segments SET start_ms = NULL WHERE start_ms < 0")
    )
    bind.execute(
        sa.text("UPDATE transcript_segments SET end_ms = NULL WHERE end_ms < 0")
    )
    bind.execute(
        sa.text(
            "UPDATE transcript_segments SET start_ms = NULL, end_ms = NULL "
            "WHERE start_ms IS NOT NULL AND end_ms IS NOT NULL AND start_ms > end_ms"
        )
    )
    # 3e. 产物关联类型白名单外 → 删除（仅链接行）
    bind.execute(
        sa.text(
            "DELETE FROM artifact_evidence WHERE artifact_type NOT IN "
            "('note_version','knowledge_point','worked_example','learning_task',"
            "'decision','summary')"
        )
    )

    # 4. 转写片段时间合法约束
    op.create_check_constraint(
        "ck_transcript_segment_start_nonneg",
        "transcript_segments",
        "start_ms IS NULL OR start_ms >= 0",
    )
    op.create_check_constraint(
        "ck_transcript_segment_end_nonneg",
        "transcript_segments",
        "end_ms IS NULL OR end_ms >= 0",
    )
    op.create_check_constraint(
        "ck_transcript_segment_time_order",
        "transcript_segments",
        "start_ms IS NULL OR end_ms IS NULL OR start_ms <= end_ms",
    )

    # 5. 证据定位 / 时间约束
    op.create_check_constraint(
        "ck_evidence_span_transcript_span",
        "evidence_spans",
        "transcript_version_id IS NULL OR "
        "(segment_start IS NOT NULL AND segment_end IS NOT NULL "
        " AND segment_start <= segment_end)",
    )
    op.create_check_constraint(
        "ck_evidence_span_document_block",
        "evidence_spans",
        "document_version_id IS NULL OR block_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_evidence_span_start_nonneg",
        "evidence_spans",
        "start_ms IS NULL OR start_ms >= 0",
    )
    op.create_check_constraint(
        "ck_evidence_span_end_nonneg",
        "evidence_spans",
        "end_ms IS NULL OR end_ms >= 0",
    )
    op.create_check_constraint(
        "ck_evidence_span_time_order",
        "evidence_spans",
        "start_ms IS NULL OR end_ms IS NULL OR start_ms <= end_ms",
    )
    # 单来源约束（放宽版）：合法证据必须且只能有单一来源；隔离证据（is_valid=FALSE）可无来源
    op.create_check_constraint(
        "ck_evidence_span_single_source",
        "evidence_spans",
        "(is_valid = FALSE) OR "
        "((transcript_version_id IS NOT NULL) != (document_version_id IS NOT NULL))",
    )

    # 6. 产物关联类型白名单
    op.create_check_constraint(
        "ck_artifact_evidence_type",
        "artifact_evidence",
        "artifact_type IN ('note_version','knowledge_point','worked_example',"
        "'learning_task','decision','summary')",
    )

    # 7. 增量幂等回填（F2）：保留既有合法产物，仅补建缺失版本
    backfill_legacy_course(bind, settings.LOCAL_SUBJECT_ID)
    report = backfill_transcript_versions(bind, settings.LOCAL_SUBJECT_ID)
    log.info("classroom phase1 backfill report: %s", report)


def downgrade() -> None:
    # 有损降级：仅撤销结构与约束，回填数据不恢复。
    op.drop_constraint("ck_artifact_evidence_type", "artifact_evidence", type_="check")
    op.drop_constraint("ck_evidence_span_time_order", "evidence_spans", type_="check")
    op.drop_constraint("ck_evidence_span_end_nonneg", "evidence_spans", type_="check")
    op.drop_constraint("ck_evidence_span_start_nonneg", "evidence_spans", type_="check")
    op.drop_constraint("ck_evidence_span_document_block", "evidence_spans", type_="check")
    op.drop_constraint("ck_evidence_span_transcript_span", "evidence_spans", type_="check")
    # 恢复 e5 的严格单来源约束（撤销隔离放宽）
    op.drop_constraint("ck_evidence_span_single_source", "evidence_spans", type_="check")
    op.create_check_constraint(
        "ck_evidence_span_single_source",
        "evidence_spans",
        "(transcript_version_id IS NOT NULL) != (document_version_id IS NOT NULL)",
    )
    op.drop_constraint("ck_transcript_segment_time_order", "transcript_segments", type_="check")
    op.drop_constraint("ck_transcript_segment_end_nonneg", "transcript_segments", type_="check")
    op.drop_constraint("ck_transcript_segment_start_nonneg", "transcript_segments", type_="check")
    op.drop_column("transcript_segments", "source_transcript_id")
    op.alter_column(
        "audio_assets",
        "storage_key",
        existing_type=sa.String(length=500),
        nullable=False,
    )
