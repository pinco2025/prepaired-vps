"""
SQLAlchemy ORM models for the questions database.
Mirrors SCHEMA.sql exactly — do NOT add columns here without a matching Alembic migration.

CRITICAL ARCHITECTURE NOTE: This database strictly stores content (questions, solutions).
User, Auth, and Test session data remains permanently hosted online on Supabase. These 
services will not be merged for the foreseeable future, so do not add user models here.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import uuid6
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Question(Base):
    __tablename__ = "questions"

    # ── Primary key ────────────────────────────────────────────────────────────
    # UUID v7 generated Python-side (time-ordered). DB server_default is a
    # fallback only — in practice Python always provides the id before INSERT.
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid6.uuid7()),
    )

    # ── Scalar columns ─────────────────────────────────────────────────────────
    legacy_id: Mapped[Optional[str]] = mapped_column(Text, unique=True, index=True)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[Optional[str]] = mapped_column(String(50))
    year: Mapped[Optional[int]] = mapped_column(SmallInteger)
    subject: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    chapter: Mapped[Optional[str]] = mapped_column(String(20), index=True)

    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    globally_open: Mapped[Optional[bool]] = mapped_column(Boolean)
    used_in: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text))

    # ── JSONB columns ──────────────────────────────────────────────────────────
    # {"text": str, "image_url": str | null}
    question: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # {"A": {"text": str, "image_url": str|null}, "B": ..., "C": ..., "D": ...}
    options: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # {"source_code": str, "source_q_no": str, "difficulty": "E"|"M"|"H",
    #  "section_type": str, "legacy_table": str}
    source_info: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)

    # {"scary": bool, "calc": bool, "multi_concept": bool}
    flags: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default='{"scary": false, "calc": false, "multi_concept": false}',
    )

    # {"freq": int}
    stats: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default='{"freq": 0}'
    )

    # ── Misc ───────────────────────────────────────────────────────────────────
    cluster_assignment: Mapped[Optional[str]] = mapped_column(Text)
    links: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    solution: Mapped[Optional["Solution"]] = relationship(
        "Solution", back_populates="question", uselist=False, lazy="select"
    )


class Solution(Base):
    __tablename__ = "solutions"

    # PK is also a FK to questions.id (1-to-1)
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("questions.id", ondelete="CASCADE"),
        primary_key=True,
    )

    explanation: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    solution_image_url: Mapped[Optional[str]] = mapped_column(Text)

    # {"source_xml": str, "imported_at": str}
    additional_data: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)

    # ── Relationship ───────────────────────────────────────────────────────────
    question: Mapped["Question"] = relationship("Question", back_populates="solution")
