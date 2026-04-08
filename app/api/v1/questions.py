"""
GET /api/v1/questions            — question set fetch (replaces api/questions.js)
GET /api/v1/questions/check      — chapter existence check
GET /api/v1/questions/{uuid}     — single question (for SEO / review)
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_optional_user
from app.core.security import TokenPayload
from app.schemas.question import QuestionDetailOut, QuestionSetOut
from app.services.question_service import (
    check_chapter_exists,
    get_question_by_id,
    get_question_set,
)

router = APIRouter(prefix="/questions", tags=["questions"])

PAID_TIERS = {"lite", "ipft-01-2026"}


def _is_paid(user: Optional[TokenPayload]) -> bool:
    """
    Paid status is encoded in the Supabase JWT's `app_metadata.subscription_tier`.
    For now we detect it by reading the raw `role` claim — this is a placeholder
    until the JWT is enriched with subscription data post-migration.
    """
    if not user:
        return False
    # TODO: once JWT contains subscription_tier, read user.app_metadata["subscription_tier"]
    return user.role in PAID_TIERS


@router.get("", response_model=QuestionSetOut)
async def list_questions(
    setId: Optional[str] = Query(None, description="Practice set identifier — matched against used_in[]"),
    testId: Optional[str] = Query(None, description="Mock test identifier — matched against used_in[]"),
    subject: Optional[str] = Query(None),
    chapterCode: Optional[str] = Query(None),
    check: Optional[int] = Query(None, description="Set to 1 for existence-only check"),
    db: AsyncSession = Depends(get_db),
    user: Optional[TokenPayload] = Depends(get_optional_user),
):
    """
    Fetches a question set from Postgres.
    Replaces the 342-line `api/questions.js` serverless function.
    Free users get the first FREE_QUESTION_LIMIT questions; paid users get all, shuffled.

    Pass either setId or testId — both are matched 1-to-1 against the used_in TEXT[] column.
    """
    # Chapter existence check (lightweight, no full data fetch)
    if check == 1:
        if not chapterCode or not subject:
            raise HTTPException(status_code=400, detail="chapterCode and subject required for existence check")
        exists = await check_chapter_exists(db, chapter_code=chapterCode, subject=subject)
        if exists:
            return {"questions": [], "solutions": {}, "totalCount": 0, "isPaid": False, "exists": True}
        raise HTTPException(status_code=404, detail="Chapter not found")

    if not setId and not testId and not chapterCode:
        raise HTTPException(status_code=400, detail="Provide setId, testId, or chapterCode")

    group_id = setId or testId
    paid = _is_paid(user)

    result = await get_question_set(
        db,
        group_id=group_id,
        subject=subject,
        chapter_code=chapterCode,
        is_paid=paid,
    )
    return result


@router.get("/{question_id}", response_model=QuestionDetailOut)
async def get_question(
    question_id: str,
    db: AsyncSession = Depends(get_db),
    user: Optional[TokenPayload] = Depends(get_optional_user),
):
    """Fetch a single question by UUID or legacy_id. Used for SEO OG meta generation."""
    detail = await get_question_by_id(db, question_id)
    if not detail:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")
    return detail
