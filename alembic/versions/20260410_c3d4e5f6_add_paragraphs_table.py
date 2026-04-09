"""Add paragraphs table and paragraph_id FK on questions

Revision ID: c3d4e5f6
Revises: b2c3d4e5
Create Date: 2026-04-10 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'c3d4e5f6'
down_revision: Union[str, None] = 'b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the paragraphs grouping table first (FK target must exist before referencing column)
    op.create_table(
        'paragraphs',
        sa.Column('id', UUID(as_uuid=False), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # Add paragraph_id FK column to questions
    op.add_column(
        'questions',
        sa.Column('paragraph_id', UUID(as_uuid=False), sa.ForeignKey('paragraphs.id', ondelete='SET NULL'), nullable=True),
    )

    # Index for efficient "give me all siblings" lookups
    op.create_index('ix_questions_paragraph_id', 'questions', ['paragraph_id'])


def downgrade() -> None:
    op.drop_index('ix_questions_paragraph_id', table_name='questions')
    op.drop_column('questions', 'paragraph_id')
    op.drop_table('paragraphs')
