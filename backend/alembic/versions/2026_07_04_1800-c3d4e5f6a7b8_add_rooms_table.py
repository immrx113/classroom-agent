"""add rooms table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-04 18:00:00+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'rooms',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('meeting_id', sa.UUID(), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('scene', sa.String(length=50), nullable=False, server_default='generic'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('sfu_router_id', sa.String(length=100), nullable=True),
        sa.Column('participants', sa.ARRAY(sa.String()), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_rooms_meeting_id', 'rooms', ['meeting_id'])
    op.create_index('ix_rooms_status', 'rooms', ['status'])
    op.create_index('ix_rooms_scene', 'rooms', ['scene'])


def downgrade() -> None:
    op.drop_index('ix_rooms_scene', table_name='rooms')
    op.drop_index('ix_rooms_status', table_name='rooms')
    op.drop_index('ix_rooms_meeting_id', table_name='rooms')
    op.drop_table('rooms')
