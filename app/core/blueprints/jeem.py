"""
JEE Main (JEEM) exam blueprint — v1.

Paper structure:
  3 subjects × 25 questions = 75 total
    div1 (MCQ, Section A): 20 per subject — sourced from ≥8 distinct chapters
    div2 (Integer, Section B):  5 per subject — sourced from ≥4 distinct chapters
  Marks: +4 per question, -1 for div1 wrong, 0 for div2 wrong → max 300

Audit invariant: every (chapter, div) cell must have ≥5 globally_open verified
questions before we consider the pool "production-ready".

To add weighted chapters (e.g., boost high-PYQ chapters): add
  chapter_weights={"ROT": 2.0, "ECF": 1.5, ...}
to the relevant SubjectBlueprint. Unspecified chapters default to 1.0.
"""

from app.core.blueprints import DivQuota, ExamBlueprint, SubjectBlueprint

JEEM_BLUEPRINT_V1 = ExamBlueprint(
    exam="JEEM",
    version="v1",
    duration_seconds=10800,
    title="JEE Main Mock Test",
    total_marks=300,
    min_pool_per_chapter_div=5,
    subjects=(
        SubjectBlueprint(
            subject="physics",
            quotas=(
                DivQuota(div="div1", count=20, min_per_chapter=1, min_chapters=8),
                DivQuota(div="div2", count=5,  min_per_chapter=1, min_chapters=4),
            ),
            chapter_weights={},
        ),
        SubjectBlueprint(
            subject="chemistry",
            quotas=(
                DivQuota(div="div1", count=20, min_per_chapter=1, min_chapters=8),
                DivQuota(div="div2", count=5,  min_per_chapter=1, min_chapters=4),
            ),
            chapter_weights={},
        ),
        SubjectBlueprint(
            subject="mathematics",
            quotas=(
                DivQuota(div="div1", count=20, min_per_chapter=1, min_chapters=8),
                DivQuota(div="div2", count=5,  min_per_chapter=1, min_chapters=4),
            ),
            chapter_weights={},
        ),
    ),
)

# Registry for all supported blueprints.  Add new exams here only — never mutate.
BLUEPRINTS: dict[str, ExamBlueprint] = {
    "JEEM": JEEM_BLUEPRINT_V1,
}
