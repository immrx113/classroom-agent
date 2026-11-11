"""add realtime_sessions table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-04 12:00:00+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'realtime_sessions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('meeting_id', sa.UUID(), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('participants', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('segment_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_realtime_sessions_meeting_id', 'realtime_sessions', ['meeting_id'])
    op.create_index('ix_realtime_sessions_status', 'realtime_sessions', ['status'])


def downgrade() -> None:
    op.drop_index('ix_realtime_sessions_status', table_name='realtime_sessions')
    op.drop_index('ix_realtime_sessions_meeting_id', table_name='realtime_sessions')
    op.drop_table('realtime_sessions')
