"""add_scope_ownership

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-10-27

阶段一复验 F1：给无主体归属的资源补主体归属，取消「NULL 关联即所有人可见」。

- chat_sessions.owner_id：全局会话（meeting_id 为空）同样有归属。
- knowledge_documents.owner_id：知识块按主体隔离。

回填策略：能经 meeting → course 推断归属的用课程 owner；否则落到本地主体
（历史数据仅有本地主体），随后列收紧为 NOT NULL。
"""

from alembic import op
import sqlalchemy as sa

from app.config import settings

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    local_owner = settings.LOCAL_SUBJECT_ID

    # 1. chat_sessions.owner_id
    op.add_column(
        "chat_sessions",
        sa.Column("owner_id", sa.String(length=100), nullable=True),
    )
    bind.execute(
        sa.text(
            "UPDATE chat_sessions cs SET owner_id = c.owner_id "
            "FROM meetings m JOIN courses c ON m.course_id = c.id "
            "WHERE cs.meeting_id = m.id"
        )
    )
    bind.execute(
        sa.text("UPDATE chat_sessions SET owner_id = :o WHERE owner_id IS NULL"),
        {"o": local_owner},
    )
    op.alter_column("chat_sessions", "owner_id", nullable=False)
    op.create_index("idx_chat_sessions_owner", "chat_sessions", ["owner_id"])

    # 2. knowledge_documents.owner_id
    op.add_column(
        "knowledge_documents",
        sa.Column("owner_id", sa.String(length=100), nullable=True),
    )
    bind.execute(
        sa.text(
            "UPDATE knowledge_documents kd SET owner_id = c.owner_id "
            "FROM meetings m JOIN courses c ON m.course_id = c.id "
            "WHERE kd.source_id = m.id"
        )
    )
    bind.execute(
        sa.text("UPDATE knowledge_documents SET owner_id = :o WHERE owner_id IS NULL"),
        {"o": local_owner},
    )
    op.alter_column("knowledge_documents", "owner_id", nullable=False)
    op.create_index("idx_knowledge_documents_owner", "knowledge_documents", ["owner_id"])


def downgrade() -> None:
    op.drop_index("idx_knowledge_documents_owner", table_name="knowledge_documents")
    op.drop_column("knowledge_documents", "owner_id")
    op.drop_index("idx_chat_sessions_owner", table_name="chat_sessions")
    op.drop_column("chat_sessions", "owner_id")
