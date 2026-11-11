"""add_room_meeting_fk

Revision ID: 293138585702
Revises: b9c2d3e4f5a6
Create Date: 2026-07-15 13:18:17.955700+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '293138585702'
down_revision: Union[str, None] = 'b9c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('ALTER TABLE rooms DROP CONSTRAINT IF EXISTS rooms_meeting_id_fkey')
    op.create_foreign_key(
        'rooms_meeting_id_fkey',
        'rooms',
        'meetings',
        ['meeting_id'],
        ['id'],
        ondelete='SET NULL'
    )


def downgrade() -> None:
    op.drop_constraint('rooms_meeting_id_fkey', 'rooms', type_='foreignkey')
