"""add agent_runs table

Revision ID: a1b2c3d4e5f6
Revises: 49d34cdead0c
Create Date: 2026-07-03 10:00:00+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '49d34cdead0c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_runs',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('meeting_id', sa.UUID(), nullable=False),
        sa.Column('graph_name', sa.String(length=100), nullable=False, server_default='meeting_summary_graph_v2'),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='pending'),
        sa.Column('current_node', sa.String(length=100), nullable=True),
        sa.Column('plan', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_cost_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('max_tokens', sa.Integer(), nullable=False, server_default='50000'),
        sa.Column('max_cost_usd', sa.Float(), nullable=False, server_default='0.5'),
        sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('node_usage', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('tool_calls', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('thread_id', sa.String(length=100), nullable=True),
        sa.Column('checkpoint_id', sa.String(length=200), nullable=True),
        sa.Column('review_status', sa.String(length=50), nullable=True),
        sa.Column('reviewer', sa.String(length=100), nullable=True),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_agent_runs_meeting_id', 'agent_runs', ['meeting_id'])
    op.create_index('ix_agent_runs_status', 'agent_runs', ['status'])


def downgrade() -> None:
    op.drop_index('ix_agent_runs_status', table_name='agent_runs')
    op.drop_index('ix_agent_runs_meeting_id', table_name='agent_runs')
    op.drop_table('agent_runs')
