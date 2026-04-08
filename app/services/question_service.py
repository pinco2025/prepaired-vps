"""
Question service — all business logic for fetching questions from Postgres.

Design principles:
- Single query per request (no N+1)
- Eager-load solution only when caller asks
- Tier enforcement (free limit) done here, not in the router
- Output normalisation: converts ORM rows → Pydantic shapes expected by frontend
"""

import random
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.question import Question, Solution
from app.schemas.question import (
    Flags,
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
    )


def _orm_to_solution_out(s: Optional[Solution]) -> Optional[SolutionOut]:
    if not s:
        return None
    return SolutionOut(
        explanation=s.explanation or "",
        solution_image_url=s.solution_image_url,
    )


# ── Public service functions ───────────────────────────────────────────────────

async def get_question_set(
    db: AsyncSession,
    *,
    group_id: Optional[str] = None,
    subject: Optional[str] = None,
    chapter_code: Optional[str] = None,
    is_paid: bool = False,
) -> QuestionSetOut:
    """
    Fetch questions (and their solutions) for the given group/subject/chapter
    combination.  Paid users get all questions shuffled; free users get the
    first FREE_QUESTION_LIMIT in original order.

    group_id — any set or test identifier; matched against the used_in TEXT[]
               column using array containment (used_in @> ARRAY[group_id]).
    """
    stmt = (
        select(Question)
        .options(selectinload(Question.solution))
    )

    # ── Filtering ──────────────────────────────────────────────────────────────
    if group_id:
        stmt = stmt.where(Question.used_in.contains([group_id]))
    if chapter_code:
        stmt = stmt.where(Question.chapter == chapter_code)
    if subject:
        stmt = stmt.where(Question.subject == subject.lower())

    # Only serve questions that passed verification
    stmt = stmt.where(Question.verification_status == "verified")

    result = await db.execute(stmt)
    all_questions: List[Question] = list(result.scalars().all())
    total_count = len(all_questions)

    # ── Tier enforcement ───────────────────────────────────────────────────────
    if is_paid:
        selected = random.sample(all_questions, len(all_questions))  # shuffle
    else:
        selected = all_questions[: settings.FREE_QUESTION_LIMIT]

    selected_ids = {q.id for q in selected}

    # ── Build response ─────────────────────────────────────────────────────────
    questions_out = [_orm_to_question_out(q, expose_answer=False) for q in selected]

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
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


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
