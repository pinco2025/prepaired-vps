"""
Marked questions endpoints — replaces markedQuestionsService.ts Supabase calls.

POST   /api/v1/marked-questions                 — mark a question (upsert)
DELETE /api/v1/marked-questions/{question_uuid} — unmark a question
GET    /api/v1/marked-questions                 — list UUIDs (optional ?subject=X)
GET    /api/v1/marked-questions/counts          — per-subject counts
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import get_current_user
from app.core.security import TokenPayload
from app.schemas.misc import MarkedCountsOut, MarkedQuestionIn
from app.services.supabase_client import sb_delete, sb_select, sb_upsert

router = APIRouter(prefix="/marked-questions", tags=["marked-questions"])


# Static-path route must come before /{question_uuid}
@router.get("/counts", response_model=MarkedCountsOut)
async def get_marked_counts(user: TokenPayload = Depends(get_current_user)):
    rows = await sb_select(
        "marked_questions",
        {"user_id": f"eq.{user.sub}"},
        select_cols="subject",
    )
    counts: Dict[str, int] = {}
    for r in rows:
        s = r["subject"]
        counts[s] = counts.get(s, 0) + 1
    return MarkedCountsOut(counts=counts)


@router.post("", status_code=status.HTTP_201_CREATED)
async def mark_question(
    body: MarkedQuestionIn,
    user: TokenPayload = Depends(get_current_user),
):
    await sb_upsert(
        "marked_questions",
        {"user_id": user.sub, "question_uuid": body.question_uuid, "subject": body.subject},
        on_conflict="user_id,question_uuid",
    )
    return {"status": "ok"}


@router.delete("/{question_uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def unmark_question(
    question_uuid: str,
    user: TokenPayload = Depends(get_current_user),
):
    await sb_delete(
        "marked_questions",
        {"user_id": f"eq.{user.sub}", "question_uuid": f"eq.{question_uuid}"},
    )


@router.get("", response_model=List[str])
async def get_marked_uuids(
    subject: Optional[str] = Query(default=None),
    user: TokenPayload = Depends(get_current_user),
):
    filters = {"user_id": f"eq.{user.sub}"}
    if subject:
        filters["subject"] = f"eq.{subject}"
    rows = await sb_select(
        "marked_questions",
        filters,
        select_cols="question_uuid",
        order="created_at.desc",
    )
    return [r["question_uuid"] for r in rows]
