"""
Question service — all business logic for fetching questions from Postgres.

Design principles:
- Single query per request (no N+1)
- Eager-load solution only when caller asks
- Tier enforcement (free limit) done here, not in the router
- Output normalisation: converts ORM rows → Pydantic shapes expected by frontend
"""

import logging
import random
from typing import Any, Dict, List, NamedTuple, Optional

logger = logging.getLogger(__name__)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.question import Paragraph, Question, Solution
from app.services.supabase_client import sb_select
from app.schemas.question import (
    Flags,
    JEEMQuestionOut,
    JEEMQuestionTags,
    JEEMSectionConfig,
    JEEMTestOut,
    OptionContent,
    QuestionDetailOut,
    QuestionOut,
    QuestionSetOut,
    SolutionOut,
    SourceInfo,
)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _orm_to_question_out(q: Question, expose_answer: bool = False) -> QuestionOut:
    """Convert a Question ORM row to the Pydantic frontend shape."""
    question_json = q.question or {}
    options_json: Dict = q.options or {}

    # Normalise options: JSONB stores {"A": {text, image_url}, ...}
    # Frontend expects: [{id: "a", text: ..., image: ...}, ...]
    options_list = []
    for key in ("A", "B", "C", "D"):
        opt = options_json.get(key) or {}
        if opt.get("text") or opt.get("image_url"):
            options_list.append({
                "id": key.lower(),
                "text": opt.get("text", ""),
                "image": opt.get("image_url"),
            })

    return QuestionOut(
        id=q.legacy_id or q.id,
        uuid=q.legacy_id or q.id,
        text=question_json.get("text", ""),
        image=question_json.get("image_url"),
        options=options_list,
        correctAnswer=q.answer if expose_answer else None,
        chapterCode=q.chapter,
        year=q.year,
        type=q.type,
        subject=q.subject,
        flags=Flags(**(q.flags or {})) if q.flags else None,
        source_info=SourceInfo(**(q.source_info or {})) if q.source_info else None,
        paragraph_id=q.paragraph_id,
    )


def _orm_to_solution_out(s: Optional[Solution]) -> Optional[SolutionOut]:
    if not s:
        return None
    return SolutionOut(
        explanation=s.explanation or "",
        solution_image_url=s.solution_image_url,
    )


# ── Paragraph helpers ─────────────────────────────────────────────────────────

async def _expand_paragraph_siblings(
    db: AsyncSession,
    questions: List[Question],
) -> List[Question]:
    """
    For any question in the list that has a paragraph_id, fetch all sibling
    questions sharing that paragraph_id that are not already in the list.
    Returns a new list with siblings inserted immediately after the first
    member of their group that appears in the input.
    """
    para_ids = {q.paragraph_id for q in questions if q.paragraph_id}
    if not para_ids:
        return questions

    existing_ids = {q.id for q in questions}
    stmt = (
        select(Question)
        .options(selectinload(Question.solution))
        .where(Question.paragraph_id.in_(para_ids))
        .where(Question.id.notin_(existing_ids))
        .where(Question.verification_status == "verified")
    )
    result = await db.execute(stmt)
    siblings: List[Question] = list(result.scalars().all())

    if not siblings:
        return questions

    sibling_map: Dict[str, List[Question]] = {}
    for s in siblings:
        sibling_map.setdefault(s.paragraph_id, []).append(s)

    result_list: List[Question] = []
    seen_para_ids: set = set()
    for q in questions:
        result_list.append(q)
        if q.paragraph_id and q.paragraph_id not in seen_para_ids:
            seen_para_ids.add(q.paragraph_id)
            result_list.extend(sibling_map.get(q.paragraph_id, []))

    return result_list


def _shuffle_preserving_paragraphs(questions: List[Question]) -> List[Question]:
    """
    Shuffle questions while keeping paragraph siblings contiguous.
    Each paragraph group (or individual non-paragraph question) is treated as
    a single unit for shuffling purposes.
    """
    groups: List[List[Question]] = []
    para_groups: Dict[str, List[Question]] = {}

    for q in questions:
        if q.paragraph_id:
            if q.paragraph_id not in para_groups:
                group: List[Question] = []
                para_groups[q.paragraph_id] = group
                groups.append(group)
            para_groups[q.paragraph_id].append(q)
        else:
            groups.append([q])

    random.shuffle(groups)
    return [q for group in groups for q in group]


# ── Public service functions ───────────────────────────────────────────────────

async def get_mcq_set(
    db: AsyncSession,
    *,
    group_id: Optional[str] = None,
    subject: Optional[str] = None,
    chapter_code: Optional[str] = None,
    chapter_codes: Optional[List[str]] = None,
    is_paid: bool = False,
    div1_only: bool = False,
) -> QuestionSetOut:
    """
    Fetch MCQ-type questions (and their solutions) for the given group/subject/chapter
    combination.  Paid users get all questions shuffled; free users get the
    first FREE_QUESTION_LIMIT in original order.

    group_id     — any set identifier; matched against the used_in TEXT[] column
                   using array containment (used_in @> ARRAY[group_id]).
    chapter_codes — list of chapter codes for section-level practice (e.g. ADV sections).
                   Takes precedence over chapter_code when both are provided.
    div1_only    — when True, strips div2 (Integer) questions by checking
                   source_info.section_type; used for chapter-based practice sets.
    """
    stmt = (
        select(Question)
        .options(selectinload(Question.solution))
    )

    # ── Filtering ──────────────────────────────────────────────────────────────
    if group_id:
        stmt = stmt.where(Question.used_in.contains([group_id]))
    if chapter_codes:
        stmt = stmt.where(Question.chapter.in_(chapter_codes))
    elif chapter_code:
        stmt = stmt.where(Question.chapter == chapter_code)
    if subject:
        stmt = stmt.where(Question.subject == subject.lower())

    # Only serve questions that passed verification
    stmt = stmt.where(Question.verification_status == "verified")

    result = await db.execute(stmt)
    all_questions: List[Question] = list(result.scalars().all())

    # For chapter-based sets, exclude Integer/div2 questions — only MCQ (div1)
    if div1_only:
        all_questions = [
            q for q in all_questions
            if _normalise_div((q.source_info or {}).get("section_type")) != "div2"
        ]

    # Expand paragraph groups: if any div5 question is present, fetch its siblings
    all_questions = await _expand_paragraph_siblings(db, all_questions)

    total_count = len(all_questions)

    # ── Tier enforcement ───────────────────────────────────────────────────────
    if is_paid:
        selected = _shuffle_preserving_paragraphs(all_questions)
    else:
        # Slice to free limit, but extend to include the complete last paragraph group
        limit = settings.FREE_QUESTION_LIMIT
        selected = all_questions[:limit]
        if selected and selected[-1].paragraph_id:
            last_para_id = selected[-1].paragraph_id
            for q in all_questions[limit:]:
                if q.paragraph_id == last_para_id:
                    selected.append(q)
                else:
                    break

    selected_ids = {q.id for q in selected}

    # ── Build response ─────────────────────────────────────────────────────────
    questions_out = [_orm_to_question_out(q, expose_answer=True) for q in selected]

    solutions_out: Dict[str, SolutionOut] = {}
    for q in selected:
        sol = _orm_to_solution_out(q.solution)
        if sol:
            key = q.legacy_id or q.id
            solutions_out[key] = sol

    return QuestionSetOut(
        questions=questions_out,
        solutions=solutions_out,
        totalCount=total_count,
        isPaid=is_paid,
    )


async def get_question_by_id(
    db: AsyncSession,
    question_id: str,
    *,
    include_answer: bool = False,
) -> Optional[QuestionDetailOut]:
    """
    Fetch a single question by UUID or legacy_id.
    Used for /api/v1/questions/{uuid} (SEO + review).
    """
    stmt = (
        select(Question)
        .options(selectinload(Question.solution))
        .where(
            (Question.id == question_id) | (Question.legacy_id == question_id)
        )
    )
    result = await db.execute(stmt)
    q: Optional[Question] = result.scalar_one_or_none()
    if not q:
        return None

    return QuestionDetailOut(
        question=_orm_to_question_out(q, expose_answer=include_answer),
        solution=_orm_to_solution_out(q.solution),
    )


async def check_chapter_exists(
    db: AsyncSession,
    *,
    chapter_code: str,
    subject: str,
) -> bool:
    """Lightweight existence check used by ChapterDetails.tsx before full fetch."""
    stmt = (
        select(Question.id)
        .where(Question.chapter == chapter_code)
        .where(Question.subject == subject.lower())
        .where(Question.verification_status == "verified")
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


# ── JEEM output shaping ────────────────────────────────────────────────────────

class _DivSpec(NamedTuple):
    suffix: str       # "Section A" | "Section B"
    q_type: str       # "MCQ" | "Integer"
    marks: int        # positive marks per question
    neg_marks: int    # negative marks per question (0 or negative)
    expected: int     # expected question count — used for data-quality warnings


# Section definitions for JEE Main output type.
# Order determines the render order in the test UI.
_JEEM_SUBJECTS: List[str] = ["physics", "chemistry", "mathematics"]
_JEEM_SUBJECT_LABELS: Dict[str, str] = {
    "physics": "Physics",
    "chemistry": "Chemistry",
    "mathematics": "Mathematics",
}
_JEEM_DIV_CONFIG: Dict[str, _DivSpec] = {
    "div1": _DivSpec(suffix="Section A", q_type="MCQ",          marks=4, neg_marks=-1, expected=20),
    "div2": _DivSpec(suffix="Section B", q_type="Integer",      marks=4, neg_marks=0,  expected=5),
    "div3": _DivSpec(suffix="Section B", q_type="Decimal",      marks=4, neg_marks=0,  expected=0),  # decimal/numerical — same section as div2
    "div4": _DivSpec(suffix="Section D", q_type="MatrixMatch",  marks=4, neg_marks=-1, expected=0),  # matrix matching
    "div5": _DivSpec(suffix="Section A", q_type="Paragraph",    marks=4, neg_marks=-1, expected=0),  # paragraph-based — same section as div1
    "div8": _DivSpec(suffix="Section C", q_type="MultiCorrect", marks=4, neg_marks=-2, expected=0),
}

# Built once at import time — purely derived from the constants above.
_JEEM_SECTIONS: List[JEEMSectionConfig] = [
    JEEMSectionConfig(
        name=f"{_JEEM_SUBJECT_LABELS[subj]} - {spec.suffix}",
        marksPerQuestion=spec.marks,
        negativeMarksPerQuestion=spec.neg_marks,
    )
    for subj in _JEEM_SUBJECTS
    for spec in (_JEEM_DIV_CONFIG["div1"], _JEEM_DIV_CONFIG["div2"])
]


def _normalise_div(raw: Optional[str]) -> Optional[str]:
    """Map section_type raw values to canonical div keys (div1 / div2 / div3 / div4 / div5 / div8)."""
    if not raw:
        return None
    v = raw.strip().lower()
    if v in ("div1", "d1", "section_a", "sec_a", "sectiona"):
        return "div1"
    if v in ("div2", "d2", "section_b", "sec_b", "sectionb"):
        return "div2"
    if v in ("div3", "d3", "decimal", "numerical", "section_c", "sec_c"):
        return "div3"
    if v in ("div4", "d4", "matrix", "matrix_match", "matrix_matching", "matching"):
        return "div4"
    if v in ("div5", "d5", "paragraph", "comprehension", "para"):
        return "div5"
    if v in ("div8", "d8", "multi_correct", "multicorrect", "multiple_correct"):
        return "div8"
    if v.startswith("div1") or v.startswith("d1"):
        return "div1"
    if v.startswith("div2") or v.startswith("d2"):
        return "div2"
    if v.startswith("div3") or v.startswith("d3"):
        return "div3"
    if v.startswith("div4") or v.startswith("d4"):
        return "div4"
    if v.startswith("div5") or v.startswith("d5"):
        return "div5"
    if v.startswith("div8") or v.startswith("d8"):
        return "div8"
    return None


def _orm_to_jeem_question_out(
    q: Question,
    section_name: str,
    question_type: str,
    marks: int,
) -> JEEMQuestionOut:
    question_json = q.question or {}
    options_json: Dict = q.options or {}
    source_info = q.source_info or {}

    options_list = []
    for key in ("A", "B", "C", "D"):
        opt = options_json.get(key) or {}
        if opt.get("text") or opt.get("image_url"):
            options_list.append({
                "id": key.lower(),
                "text": opt.get("text", ""),
                "image": opt.get("image_url"),
            })

    return JEEMQuestionOut(
        id=q.legacy_id or q.id,
        uuid=q.legacy_id or q.id,
        text=question_json.get("text", ""),
        image=question_json.get("image_url"),
        options=options_list,
        correctAnswer=q.answer,           # always exposed — score service needs it
        marks=marks,
        section=section_name,
        chapterCode=q.chapter,
        difficulty=source_info.get("difficulty"),
        questionType=question_type,       # "MCQ" | "Integer" | "MultiCorrect" — read by score_service
        tags=JEEMQuestionTags(
            tag1=source_info.get("source_code", "") or "",
            tag2=q.chapter or "",          # score_service reads tags.tag2 for chapter breakdown
            tag3="",
            tag4=source_info.get("source_q_no", "") or "",
            type=question_type,
            year=str(q.year or ""),
        ),
    )


async def get_jeem_test(
    db: AsyncSession,
    *,
    test_id: str,
    test_title: str = "JEE Main Mock Test",
    duration: int = 10800,             # 3 hours in seconds
    include_solutions: bool = False,
) -> JEEMTestOut:
    """
    Fetch all questions for a JEE Main test (identified by test_id in used_in[])
    and structure them into the 6-section JEEM format:
        Physics A (20 MCQ) | Physics B (5 Integer)
        Chemistry A (20 MCQ) | Chemistry B (5 Integer)
        Mathematics A (20 MCQ) | Mathematics B (5 Integer)

    section_type in source_info determines div1 (Section A) vs div2 (Section B).
    Output matches the Test interface in client/src/utils/testData.ts — store the
    endpoint URL as tests.url in Supabase for seamless frontend/scorer integration.
    """
    stmt = select(Question).where(
        Question.used_in.contains([test_id]),
        Question.verification_status == "verified",
    )
    if include_solutions:
        stmt = stmt.options(selectinload(Question.solution))
    result = await db.execute(stmt)
    all_questions: List[Question] = list(result.scalars().all())

    # ── Bucket questions into (subject, div) slots ────────────────────────────
    buckets: Dict[str, Dict[str, List[Question]]] = {
        subj: {"div1": [], "div2": []} for subj in _JEEM_SUBJECTS
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
        logger.warning(
            "JEEM shaping: %d questions had unrecognised subject/div and were excluded. "
            "test_id=%s", len(ungrouped), test_id
        )

    # ── Build ordered question list; track marks in one pass ─────────────────
    questions_out: List[JEEMQuestionOut] = []
    total_marks = 0

    for subj in _JEEM_SUBJECTS:
        label = _JEEM_SUBJECT_LABELS[subj]
        for div_key in ("div1", "div2"):
            spec = _JEEM_DIV_CONFIG[div_key]
            section_name = f"{label} - {spec.suffix}"
            section_qs = buckets[subj][div_key]

            if len(section_qs) != spec.expected:
                logger.warning(
                    "JEEM shaping: %s has %d questions, expected %d. test_id=%s",
                    section_name, len(section_qs), spec.expected, test_id,
                )

            for q in section_qs:
                questions_out.append(
                    _orm_to_jeem_question_out(q, section_name, spec.q_type, spec.marks)
                )
                total_marks += spec.marks

    solutions_out: Dict[str, SolutionOut] = {}
    if include_solutions:
        for q in all_questions:
            sol = _orm_to_solution_out(q.solution)
            if sol:
                solutions_out[q.legacy_id or q.id] = sol

    return JEEMTestOut(
        testId=test_id,
        title=test_title,
        duration=duration,
        totalMarks=total_marks,
        sections=_JEEM_SECTIONS,
        questions=questions_out,
        solutions=solutions_out,
    )


# ── NEET output shaping ────────────────────────────────────────────────────────

_NEET_SUBJECTS: List[str] = ["physics", "chemistry", "zoology", "botany"]
_NEET_SUBJECT_LABELS: Dict[str, str] = {
    "physics": "Physics",
    "chemistry": "Chemistry",
    "zoology": "Zoology",
    "botany": "Botany",
}
# NEET has only MCQ (div1), 45 per subject, +4/-1
_NEET_SPEC = _DivSpec(suffix="", q_type="MCQ", marks=4, neg_marks=-1, expected=45)

_NEET_SECTIONS: List[JEEMSectionConfig] = [
    JEEMSectionConfig(
        name=_NEET_SUBJECT_LABELS[subj],
        marksPerQuestion=_NEET_SPEC.marks,
        negativeMarksPerQuestion=_NEET_SPEC.neg_marks,
    )
    for subj in _NEET_SUBJECTS
]


async def get_neet_test(
    db: AsyncSession,
    *,
    test_id: str,
    test_title: str = "NEET Mock Test",
    duration: int = 12000,              # 200 minutes in seconds
    include_solutions: bool = False,
) -> JEEMTestOut:
    """
    Fetch all questions for a NEET test and structure into 4 sections:
        Physics (45 MCQ) | Chemistry (45 MCQ) | Zoology (45 MCQ) | Botany (45 MCQ)

    All NEET questions are MCQ (div1). Bucketed by subject only — no div split.
    Total: 180 questions, 720 marks, 200 minutes.
    """
    stmt = select(Question).where(
        Question.used_in.contains([test_id]),
        Question.verification_status == "verified",
    )
    if include_solutions:
        stmt = stmt.options(selectinload(Question.solution))
    result = await db.execute(stmt)
    all_questions: List[Question] = list(result.scalars().all())

    buckets: Dict[str, List[Question]] = {subj: [] for subj in _NEET_SUBJECTS}
    ungrouped: List[Question] = []

    for q in all_questions:
        subj = (q.subject or "").strip().lower()
        if subj in buckets:
            buckets[subj].append(q)
        else:
            ungrouped.append(q)

    if ungrouped:
        logger.warning(
            "NEET shaping: %d questions had unrecognised subject and were excluded. "
            "test_id=%s", len(ungrouped), test_id
        )

    questions_out: List[JEEMQuestionOut] = []
    total_marks = 0

    for subj in _NEET_SUBJECTS:
        label = _NEET_SUBJECT_LABELS[subj]
        section_qs = buckets[subj]

        if len(section_qs) != _NEET_SPEC.expected:
            logger.warning(
                "NEET shaping: %s has %d questions, expected %d. test_id=%s",
                label, len(section_qs), _NEET_SPEC.expected, test_id,
            )

        for q in section_qs:
            questions_out.append(
                _orm_to_jeem_question_out(q, label, _NEET_SPEC.q_type, _NEET_SPEC.marks)
            )
            total_marks += _NEET_SPEC.marks

    solutions_out: Dict[str, SolutionOut] = {}
    if include_solutions:
        for q in all_questions:
            sol = _orm_to_solution_out(q.solution)
            if sol:
                solutions_out[q.legacy_id or q.id] = sol

    return JEEMTestOut(
        testId=test_id,
        title=test_title,
        duration=duration,
        totalMarks=total_marks,
        sections=_NEET_SECTIONS,
        questions=questions_out,
        solutions=solutions_out,
    )


# ── SET output shaping ─────────────────────────────────────────────────────────

_SET_DIV_CONFIG: Dict[str, _DivSpec] = {
    "div1": _DivSpec(suffix="MCQ",          q_type="MCQ",         marks=4, neg_marks=-1, expected=0),
    "div2": _DivSpec(suffix="Integer",      q_type="Integer",     marks=4, neg_marks=0,  expected=0),
    "div3": _DivSpec(suffix="Integer",      q_type="Decimal",     marks=4, neg_marks=0,  expected=0),  # decimal — same section/marks as div2
    "div4": _DivSpec(suffix="MCQ",          q_type="MatrixMatch", marks=4, neg_marks=-1, expected=0),  # matrix matching — same section/marks as div1
    "div5": _DivSpec(suffix="MCQ",          q_type="Paragraph",   marks=4, neg_marks=-1, expected=0),  # paragraph — same section/marks as div1
    "div8": _DivSpec(suffix="MultiCorrect", q_type="MultiCorrect",marks=4, neg_marks=-2, expected=0),
}

# Built once — 3 unique sections (MCQ, Integer, MultiCorrect).
# div3 shares Integer, div4/div5 share MCQ — no duplicate sections created.
_SET_SECTIONS: List[JEEMSectionConfig] = [
    JEEMSectionConfig(
        name=spec.suffix,
        marksPerQuestion=spec.marks,
        negativeMarksPerQuestion=spec.neg_marks,
    )
    for spec in (_SET_DIV_CONFIG["div1"], _SET_DIV_CONFIG["div2"], _SET_DIV_CONFIG["div8"])
]


async def get_set_test(
    db: AsyncSession,
    *,
    test_id: str,
    test_title: str = "Practice Set",
    duration: int = 3600,               # 1 hour default
    include_solutions: bool = False,
) -> JEEMTestOut:
    """
    Fetch all questions for a practice SET and bucket into 2 sections by div type:
        MCQ (div1, +4/-1) | Integer (div2, +4/0)

    No subject grouping — section_type in source_info drives placement.
    Questions with missing/unrecognised section_type default to div1 (MCQ).
    """
    stmt = select(Question).where(
        Question.used_in.contains([test_id]),
        Question.verification_status == "verified",
    )
    if include_solutions:
        stmt = stmt.options(selectinload(Question.solution))
    result = await db.execute(stmt)
    all_questions: List[Question] = list(result.scalars().all())

    # Expand paragraph siblings before bucketing
    all_questions = await _expand_paragraph_siblings(db, all_questions)

    buckets: Dict[str, List[Question]] = {"div1": [], "div2": [], "div3": [], "div4": [], "div5": [], "div8": []}

    for q in all_questions:
        div = _normalise_div((q.source_info or {}).get("section_type")) or "div1"
        buckets[div].append(q)

    # Sort div5 bucket so paragraph siblings appear consecutive (stable sort by paragraph_id)
    buckets["div5"].sort(key=lambda q: (q.paragraph_id or "", q.id))

    questions_out: List[JEEMQuestionOut] = []
    total_marks = 0

    for div_key, spec in _SET_DIV_CONFIG.items():
        for q in buckets[div_key]:
            questions_out.append(
                _orm_to_jeem_question_out(q, spec.suffix, spec.q_type, spec.marks)
            )
            total_marks += spec.marks

    solutions_out: Dict[str, SolutionOut] = {}
    if include_solutions:
        for q in all_questions:
            sol = _orm_to_solution_out(q.solution)
            if sol:
                solutions_out[q.legacy_id or q.id] = sol

    return JEEMTestOut(
        testId=test_id,
        title=test_title,
        duration=duration,
        totalMarks=total_marks,
        sections=_SET_SECTIONS,
        questions=questions_out,
        solutions=solutions_out,
    )


# ── JEEA output shaping ────────────────────────────────────────────────────────

# Maps normalised div keys to the question type string read by score_service.
_DIV_TO_QTYPE: Dict[str, str] = {
    "div1": "MCQ",
    "div2": "Integer",
    "div3": "Decimal",
    "div4": "MatrixMatch",
    "div5": "Paragraph",
    "div8": "MultiCorrect",
}

# Preferred div ordering within each subject for rendering.
# Every div listed here gets its own section (no merging).
_JEEA_SUBJECTS: List[str] = ["physics", "chemistry", "mathematics"]
_JEEA_SUBJECT_LABELS: Dict[str, str] = {
    "physics": "Physics",
    "chemistry": "Chemistry",
    "mathematics": "Mathematics",
}
_JEEA_DIV_ORDER: List[str] = ["div1", "div5", "div8", "div4", "div2", "div3"]


async def get_jeea_test(
    db: AsyncSession,
    *,
    test_id: str,
    test_title: str = "JEE Advanced Mock Test",
    duration: int = 10800,
    include_solutions: bool = False,
) -> JEEMTestOut:
    """
    Fetch all questions for a JEE Advanced test and structure them using the
    section_config stored in the Supabase tests table.

    Unlike JEEM/NEET (which use hardcoded div configs), JEEA reads section names
    and marks directly from Supabase section_config — the same source used by
    score_service during scoring, ensuring consistency.

    Every div type (div1 MCQ, div5 Paragraph, div8 MultiCorrect, div4 MatrixMatch,
    div2 Integer, div3 Decimal) is its own section. Empty sections are omitted.

    section_config keys follow the pattern: "{subject}-{div}" e.g. "physics-div8"
    section_config values: {"name": "Physics - Multi Correct", "pos": 4, "neg": -2}
    """
    # 1. Fetch section_config from Supabase (same pattern as scores.py:68-71)
    tests_rows = await sb_select("tests", {"testID": f"eq.{test_id}"})
    if not tests_rows:
        raise ValueError(f"No test found in Supabase with testID={test_id!r}")
    section_config: Dict[str, Any] = tests_rows[0].get("section_config") or {}
    if not section_config:
        raise ValueError(
            f"tests.section_config is empty for testID={test_id!r}. "
            "Populate it before fetching the JEEA test."
        )

    # 2. Fetch questions from Postgres
    stmt = select(Question).where(
        Question.used_in.contains([test_id]),
        Question.verification_status == "verified",
    )
    if include_solutions:
        stmt = stmt.options(selectinload(Question.solution))
    result = await db.execute(stmt)
    all_questions: List[Question] = list(result.scalars().all())

    # Expand paragraph siblings so every div5 paragraph is complete
    all_questions = await _expand_paragraph_siblings(db, all_questions)

    # 3. Build buckets keyed by section_name (from section_config)
    #    Also track section metadata (marks, negMarks) by name.
    section_meta: Dict[str, Dict[str, Any]] = {}   # name → {pos, neg, q_type}
    buckets: Dict[str, List[Question]] = {}         # section_name → questions
    ungrouped: List[Question] = []

    for q in all_questions:
        subj = (q.subject or "").strip().lower()
        raw_div = (q.source_info or {}).get("section_type")
        div = _normalise_div(raw_div)
        if not div:
            ungrouped.append(q)
            continue

        section_key = f"{subj}-{div}"
        sec = section_config.get(section_key)
        if not sec:
            # Try subject-only fallback for unknown subjects
            ungrouped.append(q)
            logger.warning(
                "JEEA shaping: no section_config entry for key %r — question excluded. "
                "test_id=%s", section_key, test_id
            )
            continue

        section_name: str = sec.get("name", section_key)
        if section_name not in buckets:
            buckets[section_name] = []
            section_meta[section_name] = {
                "pos": float(sec.get("pos", 0)),
                "neg": float(sec.get("neg", 0)),
                "q_type": _DIV_TO_QTYPE.get(div, "MCQ"),
            }
        buckets[section_name].append(q)

    if ungrouped:
        logger.warning(
            "JEEA shaping: %d questions had unrecognised subject/div or missing "
            "section_config and were excluded. test_id=%s",
            len(ungrouped), test_id,
        )

    # 4. Determine section render order: group by subject, within subject by div order
    ordered_section_names: List[str] = []
    for subj in _JEEA_SUBJECTS:
        for div_key in _JEEA_DIV_ORDER:
            sec_cfg = section_config.get(f"{subj}-{div_key}")
            if not sec_cfg:
                continue
            sec_name = sec_cfg.get("name", f"{subj}-{div_key}")
            if sec_name in buckets and sec_name not in ordered_section_names:
                ordered_section_names.append(sec_name)

    # 5. Build output
    sections_out: List[JEEMSectionConfig] = []
    questions_out: List[JEEMQuestionOut] = []
    total_marks = 0

    for sec_name in ordered_section_names:
        meta = section_meta[sec_name]
        pos = int(meta["pos"])
        q_type = meta["q_type"]

        sections_out.append(JEEMSectionConfig(
            name=sec_name,
            marksPerQuestion=pos,
            negativeMarksPerQuestion=int(meta["neg"]),
        ))

        for q in buckets[sec_name]:
            questions_out.append(
                _orm_to_jeem_question_out(q, sec_name, q_type, pos)
            )
            total_marks += pos

    solutions_out: Dict[str, SolutionOut] = {}
    if include_solutions:
        for q in all_questions:
            sol = _orm_to_solution_out(q.solution)
            if sol:
                solutions_out[q.legacy_id or q.id] = sol

    return JEEMTestOut(
        testId=test_id,
        title=test_title,
        duration=duration,
        totalMarks=total_marks,
        sections=sections_out,
        questions=questions_out,
        solutions=solutions_out,
    )


async def get_diagnostic_questions(
    db: AsyncSession,
    *,
    count: int = 15,
) -> QuestionSetOut:
    """
    Return a curated sample of globally_open questions for the diagnostic quiz.
    These are always fully revealed (isPaid = True, but no correct answer exposed).
    """
    stmt = (
        select(Question)
        .options(selectinload(Question.solution))
        .where(Question.globally_open.is_(True))
        .where(Question.verification_status == "verified")
        .order_by(Question.stats["freq"].as_integer().asc())  # prefer less-used
        .limit(count * 3)   # over-fetch then sample for variety
    )
    result = await db.execute(stmt)
    pool: List[Question] = list(result.scalars().all())
    selected = random.sample(pool, min(count, len(pool)))

    questions_out = [_orm_to_question_out(q, expose_answer=False) for q in selected]
    solutions_out = {
        (q.legacy_id or q.id): sol
        for q in selected
        if (sol := _orm_to_solution_out(q.solution))
    }

    return QuestionSetOut(
        questions=questions_out,
        solutions=solutions_out,
        totalCount=len(selected),
        isPaid=True,
    )
