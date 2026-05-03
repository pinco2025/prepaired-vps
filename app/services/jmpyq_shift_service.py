"""
JMPYQ shift service — enumerate years/shifts and build JEEM-shaped test payloads
from JEE Main PYQ questions stored with source_info.source_code like "2024_30_JAN_1".

All functions that touch the DB are async and expect an AsyncSession.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.question import Question
from app.schemas.question import (
    JEEMQuestionOut,
    JEEMQuestionTags,
    JEEMSectionConfig,
    JEEMTestOut,
    SolutionOut,
)

logger = logging.getLogger(__name__)

JMPYQ_PREFIX = "jmpyq_"

_MONTH_MAP: Dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_MONTH_LABELS: Dict[str, str] = {
    "JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AUG": "Aug",
    "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec",
}

_SUBJECTS = ["physics", "chemistry", "mathematics"]
_SUBJECT_LABELS = {"physics": "Physics", "chemistry": "Chemistry", "mathematics": "Mathematics"}

# Section A: MCQ (+4/-1), Section B: Integer (+4/0) — marks per question.
# Section B count is left as-is from the data (may be 5 or 10).
_SEC_A = {"suffix": "Section A", "q_type": "MCQ",     "marks": 4, "neg_marks": -1}
_SEC_B = {"suffix": "Section B", "q_type": "Integer",  "marks": 4, "neg_marks":  0}


def parse_source_code(code: str) -> Optional[Dict[str, Any]]:
    """
    Parse a source_code string like "2024_30_JAN_1" into components.
    Returns None if the format is unrecognised.
    """
    parts = code.split("_")
    if len(parts) != 4:
        return None
    try:
        year, day, month, shift = int(parts[0]), int(parts[1]), parts[2].upper(), int(parts[3])
    except ValueError:
        return None
    if month not in _MONTH_MAP:
        return None
    return {"year": year, "day": day, "month": month, "shift": shift}


def format_shift_label(parsed: Dict[str, Any]) -> str:
    """e.g. {"year":2024,"day":30,"month":"JAN","shift":1} → "30 Jan 2024 — Shift 1" """
    return f"{parsed['day']} {_MONTH_LABELS[parsed['month']]} {parsed['year']} — Shift {parsed['shift']}"


def _chronological_key(parsed: Dict[str, Any]) -> tuple:
    return (parsed["year"], _MONTH_MAP[parsed["month"]], parsed["day"], parsed["shift"])


def build_synthetic_meta(source_code: str) -> Optional[Dict[str, Any]]:
    """
    Return a dict shaped like a curated 'tests' table row so test_resolver
    can build a TestResolution without hitting Supabase.
    Returns None if source_code format is invalid.
    """
    parsed = parse_source_code(source_code)
    if parsed is None:
        return None
    label = format_shift_label(parsed)
    return {
        "testID": f"{JMPYQ_PREFIX}{source_code}",
        "exam": "JEE",
        "title": f"JEE Main {label}",
        "duration": 10800,
        "total_marks": 300,
        "free_unlock": True,
    }


async def list_years(db: AsyncSession) -> List[int]:
    """Return distinct years (descending) for JMPYQ questions that have valid source_codes."""
    rows = await db.execute(text("""
        SELECT DISTINCT
            CAST(split_part(source_info->>'source_code', '_', 1) AS INTEGER) AS year
        FROM questions
        WHERE type = 'JMPYQ'
          AND verification_status = 'verified'
          AND source_info->>'source_code' ~ '^[0-9]{4}_'
        ORDER BY year DESC
    """))
    return [r.year for r in rows.fetchall()]


async def list_shifts(db: AsyncSession, year: int) -> List[Dict[str, Any]]:
    """
    Return all shifts for the given year, sorted chronologically.
    Each item: {source_code, label, question_count}.
    """
    rows = await db.execute(
        text("""
            SELECT
                source_info->>'source_code' AS source_code,
                COUNT(*) AS question_count
            FROM questions
            WHERE type = 'JMPYQ'
              AND verification_status = 'verified'
              AND source_info->>'source_code' LIKE :prefix
            GROUP BY source_info->>'source_code'
        """),
        {"prefix": f"{year}_%"},
    )

    shifts: List[Dict[str, Any]] = []
    for r in rows.fetchall():
        parsed = parse_source_code(r.source_code)
        if parsed is None:
            logger.warning("jmpyq: unrecognised source_code '%s' skipped", r.source_code)
            continue
        shifts.append({
            "source_code": r.source_code,
            "label": format_shift_label(parsed),
            "question_count": r.question_count,
            "_key": _chronological_key(parsed),
        })

    shifts.sort(key=lambda s: s["_key"])
    for s in shifts:
        del s["_key"]
    return shifts


def _normalise_div(raw: Optional[str]) -> Optional[str]:
    """Map source_info.section_type to 'div1' (MCQ) or 'div2' (Integer)."""
    if not raw:
        return None
    v = raw.strip().lower()
    if v in ("div1", "d1", "section_a", "sec_a", "mcq", "single", "single_correct", "sc"):
        return "div1"
    if v in ("div2", "d2", "section_b", "sec_b", "integer", "int", "integer_type"):
        return "div2"
    if v.startswith("div1") or v.startswith("d1"):
        return "div1"
    if v.startswith("div2") or v.startswith("d2"):
        return "div2"
    return None


def _orm_to_jeem_q(q: Question, section_name: str, q_type: str, marks: int) -> JEEMQuestionOut:
    question_json = q.question or {}
    options_json: Dict = q.options or {}
    source_info = q.source_info or {}

    options_list = []
    for key in ("A", "B", "C", "D"):
        opt = options_json.get(key) or {}
        if opt.get("text") or opt.get("image_url"):
            options_list.append({"id": key.lower(), "text": opt.get("text", ""), "image": opt.get("image_url")})

    return JEEMQuestionOut(
        id=q.legacy_id or q.id,
        uuid=q.legacy_id or q.id,
        text=question_json.get("text", ""),
        image=question_json.get("image_url"),
        options=options_list,
        correctAnswer=q.answer,
        marks=marks,
        section=section_name,
        chapterCode=q.chapter,
        difficulty=source_info.get("difficulty"),
        questionType=q_type,
        tags=JEEMQuestionTags(
            tag1=source_info.get("source_code", "") or "",
            tag2=q.chapter or "",
            tag3="",
            tag4=str(source_info.get("source_q_no") or ""),
            type=q_type,
            year=str(q.year or ""),
        ),
    )


async def build_jeem_test_payload(
    db: AsyncSession,
    source_code: str,
    include_solutions: bool = False,
) -> JEEMTestOut:
    """
    Fetch all JMPYQ questions for the given source_code and return a JEEMTestOut
    structured identically to what /questions/test/{id}?output_type=JEEM emits.
    Section B count is whatever the data has (5 or 10 — faithful to the original paper).
    """
    stmt = (
        select(Question)
        .where(
            Question.type == "JMPYQ",
            Question.verification_status == "verified",
            Question.source_info["source_code"].astext == source_code,
        )
    )
    if include_solutions:
        stmt = stmt.options(selectinload(Question.solution))

    result = await db.execute(stmt)
    all_questions: List[Question] = list(result.scalars().all())

    # Bucket by (subject, div)
    buckets: Dict[str, Dict[str, List[Question]]] = {
        subj: {"div1": [], "div2": []} for subj in _SUBJECTS
    }
    ungrouped: List[Question] = []
    for q in all_questions:
        subj = (q.subject or "").strip().lower()
        div = _normalise_div((q.source_info or {}).get("section_type"))
        if subj in buckets and div in buckets[subj]:
            buckets[subj][div].append(q)
        else:
            ungrouped.append(q)

    if ungrouped:
        logger.warning("jmpyq build_jeem: %d questions had unrecognised subject/div, source_code=%s", len(ungrouped), source_code)

    # Build output
    questions_out: List[JEEMQuestionOut] = []
    sections_seen: List[JEEMSectionConfig] = []
    sections_set: set = set()
    total_marks = 0

    for subj in _SUBJECTS:
        label = _SUBJECT_LABELS[subj]
        for div_key, spec in (("div1", _SEC_A), ("div2", _SEC_B)):
            sec_name = f"{label} - {spec['suffix']}"
            sec_qs = buckets[subj][div_key]
            for q in sec_qs:
                questions_out.append(_orm_to_jeem_q(q, sec_name, spec["q_type"], spec["marks"]))
                total_marks += spec["marks"]
            if sec_qs and sec_name not in sections_set:
                sections_seen.append(JEEMSectionConfig(
                    name=sec_name,
                    marksPerQuestion=spec["marks"],
                    negativeMarksPerQuestion=spec["neg_marks"],
                ))
                sections_set.add(sec_name)

    parsed = parse_source_code(source_code)
    title = f"JEE Main {format_shift_label(parsed)}" if parsed else f"JEE Main PYQ — {source_code}"

    test_id = f"{JMPYQ_PREFIX}{source_code}"
    solutions_out: Dict[str, SolutionOut] = {}
    if include_solutions:
        for q in all_questions:
            sol = q.solution
            if sol and (sol.explanation or sol.solution_image_url):
                key = q.legacy_id or q.id
                solutions_out[key] = SolutionOut(
                    explanation=sol.explanation or "",
                    solution_image_url=sol.solution_image_url,
                )

    return JEEMTestOut(
        testId=test_id,
        title=title,
        duration=10800,
        totalMarks=total_marks,
        sections=sections_seen,
        questions=questions_out,
        solutions=solutions_out,
    )


# JMPYQ section config for score_service (keyed by "{subject}-{div}")
JMPYQ_SECTION_CONFIG: Dict[str, Dict[str, Any]] = {
    "physics-div1":     {"name": "Physics - Section A",     "pos": 4.0, "neg": -1.0},
    "physics-div2":     {"name": "Physics - Section B",     "pos": 4.0, "neg":  0.0},
    "chemistry-div1":   {"name": "Chemistry - Section A",   "pos": 4.0, "neg": -1.0},
    "chemistry-div2":   {"name": "Chemistry - Section B",   "pos": 4.0, "neg":  0.0},
    "mathematics-div1": {"name": "Mathematics - Section A", "pos": 4.0, "neg": -1.0},
    "mathematics-div2": {"name": "Mathematics - Section B", "pos": 4.0, "neg":  0.0},
}
