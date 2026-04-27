"""Exam blueprint dataclasses — typed constants describing each exam's paper structure."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DivQuota:
    div: str               # canonical div key: "div1" | "div2" | ...
    count: int             # total questions to pick for this slot per subject
    min_per_chapter: int   # each selected chapter must contribute at least this many
    min_chapters: int      # at least this many distinct chapters must contribute


@dataclass(frozen=True)
class SubjectBlueprint:
    subject: str                         # lowercase: "physics" | "chemistry" | ...
    quotas: tuple[DivQuota, ...]
    # chapter_code -> relative weight (higher = more questions from that chapter).
    # An empty dict means all chapters in chapters.json are equally weighted (1.0).
    chapter_weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ExamBlueprint:
    exam: str              # "JEEM" | "NEET" | ...
    version: str           # "v1" — bump on any change to trigger re-audit
    duration_seconds: int
    title: str
    total_marks: int       # denormalised — stored in dynamic_tests at write time
    subjects: tuple[SubjectBlueprint, ...]
    # Audit threshold: every (chapter, div) cell in the DB must have at least
    # this many globally_open + verified questions, or the audit CLI flags it.
    min_pool_per_chapter_div: int
