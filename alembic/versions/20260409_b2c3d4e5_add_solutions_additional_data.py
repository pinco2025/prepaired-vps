"""Add additional_data column to solutions

Revision ID: b2c3d4e5
Revises: a1b2c3d4
Create Date: 2026-04-09 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = 'b2c3d4e5'
down_revision: Union[str, None] = 'a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('solutions', sa.Column('additional_data', JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column('solutions', 'additional_data')
