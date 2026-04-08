"""
Diagnostic quiz endpoints (Tier 6 — new functionality stubs made real).

GET    /api/v1/diagnostic/questions              — 15 curated Postgres questions
POST   /api/v1/diagnostic/submit                 — compute chapter assessments
POST   /api/v1/diagnostic/assessment             — save assessment to Supabase
GET    /api/v1/diagnostic/assessment/latest      — get latest assessment
"""

from typing import Dict

from fastapi import APIRouter, Body, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.security import TokenPayload
from app.schemas.misc import (
    DiagnosticAssessmentIn,
    DiagnosticAssessmentOut,
    DiagnosticQuizResultOut,
    DiagnosticQuizSubmitIn,
)
from app.schemas.question import QuestionSetOut
from app.services.question_service import get_diagnostic_questions, get_question_by_id
from app.services.supabase_client import sb_insert, sb_select

router = APIRouter(prefix="/diagnostic", tags=["diagnostic"])


@router.get("/questions", response_model=QuestionSetOut)
async def diagnostic_questions(
    db: AsyncSession = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
):
    """Return 15 globally_open questions from Postgres for the diagnostic quiz."""
    return await get_diagnostic_questions(db)


@router.post("/submit", response_model=DiagnosticQuizResultOut)
async def submit_diagnostic(
    body: DiagnosticQuizSubmitIn,
    db: AsyncSession = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
):
    """
    Grade submitted diagnostic answers and return per-chapter assessments.
    Scores each chapter by accuracy across its questions.
    """
    chapter_right: Dict[str, int] = {}
    chapter_total: Dict[str, int] = {}

    for q_id, submitted_answer in body.answers.items():
        detail = await get_question_by_id(db, q_id, include_answer=True)
        if not detail:
            continue
        chapter = detail.question.chapterCode or "unknown"
        chapter_total[chapter] = chapter_total.get(chapter, 0) + 1
        if submitted_answer.upper() == (detail.question.correctAnswer or "").upper():
            chapter_right[chapter] = chapter_right.get(chapter, 0) + 1

    assessments = {
        ch: round(chapter_right.get(ch, 0) / total, 2)
        for ch, total in chapter_total.items()
        if total > 0
    }
    return DiagnosticQuizResultOut(chapter_assessments=assessments)


@router.post("/assessment", response_model=DiagnosticAssessmentOut, status_code=status.HTTP_201_CREATED)
async def save_assessment(
    body: DiagnosticAssessmentIn,
    user: TokenPayload = Depends(get_current_user),
):
    row = await sb_insert("diagnostic_assessments", {
        "user_id": user.sub,
        "chapter_scores": body.chapter_scores,
        "subject": body.subject,
    })
    return DiagnosticAssessmentOut(
        id=row["id"],
        user_id=row["user_id"],
        chapter_scores=row["chapter_scores"],
        subject=row.get("subject"),
        created_at=row.get("created_at"),
    )


@router.get("/assessment/latest", response_model=DiagnosticAssessmentOut)
async def get_latest_assessment(user: TokenPayload = Depends(get_current_user)):
    rows = await sb_select(
        "diagnostic_assessments",
        {"user_id": f"eq.{user.sub}"},
        select_cols="id,user_id,chapter_scores,subject,created_at",
        order="created_at.desc",
        limit=1,
    )
    if not rows:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="No assessment found")
    r = rows[0]
    return DiagnosticAssessmentOut(**r)
