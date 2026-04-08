"""
Feedback & Question Reports endpoints — replaces feedbackService.ts Supabase calls.

POST   /api/v1/feedback                  — submit user feedback
GET    /api/v1/feedback                  — admin: list all feedback
POST   /api/v1/reports/question          — report a question
GET    /api/v1/reports                   — admin: list reports
PATCH  /api/v1/reports/{report_id}       — admin: mark resolved
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import get_current_user
from app.core.security import TokenPayload
from app.schemas.feedback import (
    FeedbackOut,
    QuestionReportOut,
    ReportQuestionIn,
    SubmitFeedbackIn,
    UpdateReportIn,
)
from app.services.supabase_client import sb_insert, sb_select, sb_update

router = APIRouter(tags=["feedback"])


# ── User feedback ──────────────────────────────────────────────────────────────

@router.post("/feedback", status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    body: SubmitFeedbackIn,
    user: TokenPayload = Depends(get_current_user),
):
    await sb_insert("user_feedback", {
        "user_id": user.sub,
        "aipt_rating": body.aipt_rating,
        "question_set_rating": body.question_set_rating,
        "ux_rating": body.ux_rating,
        "remarks": body.remarks,
    })
    return {"status": "ok"}


@router.get("/feedback", response_model=List[FeedbackOut])
async def list_feedback(user: TokenPayload = Depends(get_current_user)):
    # TODO: add admin role check once role model is defined
    rows = await sb_select(
        "user_feedback",
        {},
        select_cols="id,user_id,aipt_rating,question_set_rating,ux_rating,remarks,submitted_at",
        order="submitted_at.desc",
    )
    return rows


# ── Question reports ───────────────────────────────────────────────────────────

@router.post("/reports/question", status_code=status.HTTP_201_CREATED)
async def report_question(
    body: ReportQuestionIn,
    user: TokenPayload = Depends(get_current_user),
):
    await sb_insert("question_reports", {
        "question_uuid": body.question_id,
        "reported_parts": body.reported_parts,
        "user_id": user.sub,
        "source_url": body.source_url or "internal",
    })
    return {"status": "ok"}


@router.get("/reports", response_model=List[QuestionReportOut])
async def list_reports(user: TokenPayload = Depends(get_current_user)):
    rows = await sb_select(
        "question_reports",
        {},
        select_cols="id,question_uuid,reported_parts,user_id,reported_at,is_resolved,source_url",
        order="reported_at.desc",
    )
    return rows


@router.patch("/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_report(
    report_id: str,
    body: UpdateReportIn,
    user: TokenPayload = Depends(get_current_user),
):
    await sb_update(
        "question_reports",
        {"id": f"eq.{report_id}"},
        {"is_resolved": body.is_resolved},
        prefer_minimal=True,
    )
