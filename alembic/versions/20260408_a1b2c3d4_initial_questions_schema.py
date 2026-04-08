"""Initial questions schema

Revision ID: a1b2c3d4
Revises:
Create Date: 2026-04-08 00:00:00.000000

Creates the `questions` and `solutions` tables for the dedicated questions DB.
IMPORTANT: This DB is permanently isolated from Supabase. Do NOT add user/auth tables here.

UUID generation note:
  Uses uuid_generate_v7() from the pg_uuidv7 extension (time-ordered UUIDs).
  Requires pg_uuidv7 to be installed in your PostgreSQL instance before running.
  - Local: see installation instructions in the project README / migration plan.
  - Railway: pg_uuidv7 is available via CREATE EXTENSION.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "questions",
        sa.Column(
            "id",
            sa.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("legacy_id", sa.Text(), unique=True, nullable=True),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("type", sa.String(50), nullable=True),
        sa.Column("year", sa.SmallInteger(), nullable=True),
        sa.Column("subject", sa.String(20), nullable=True),
        sa.Column("chapter", sa.String(20), nullable=True),
        sa.Column(
            "verification_status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("globally_open", sa.Boolean(), nullable=True),
        sa.Column("used_in", ARRAY(sa.Text()), nullable=True),
        sa.Column("question", JSONB(), nullable=False),
        sa.Column("options", JSONB(), nullable=False),
        sa.Column("source_info", JSONB(), nullable=True),
        sa.Column(
            "flags",
            JSONB(),
            nullable=False,
            server_default=sa.text("""'{"scary": false, "calc": false, "multi_concept": false}'"""),
        ),
        sa.Column(
            "stats",
            JSONB(),
            nullable=False,
            server_default=sa.text("""'{"freq": 0}'"""),
        ),
        sa.Column("cluster_assignment", sa.Text(), nullable=True),
        sa.Column("links", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # Indexes matching what the ORM model declares
    op.create_index("ix_questions_legacy_id", "questions", ["legacy_id"], unique=True)
    op.create_index("ix_questions_subject", "questions", ["subject"])
    op.create_index("ix_questions_chapter", "questions", ["chapter"])
    # GIN index for JSONB metadata queries
    op.create_index(
        "ix_questions_source_info_gin",
        "questions",
        ["source_info"],
        postgresql_using="gin",
    )

    op.create_table(
        "solutions",
        sa.Column(
            "id",
            sa.UUID(as_uuid=False),
            sa.ForeignKey("questions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "explanation",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("solution_image_url", sa.Text(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("solutions")
    op.drop_index("ix_questions_source_info_gin", table_name="questions")
    op.drop_index("ix_questions_chapter", table_name="questions")
    op.drop_index("ix_questions_subject", table_name="questions")
    op.drop_index("ix_questions_legacy_id", table_name="questions")
    op.drop_table("questions")
