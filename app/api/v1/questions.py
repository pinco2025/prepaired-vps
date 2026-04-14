"""
GET /api/v1/questions            — question set fetch (replaces api/questions.js)
GET /api/v1/questions/check      — chapter existence check
GET /api/v1/questions/{uuid}     — single question (for SEO / review)
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_optional_user
from app.core.security import TokenPayload
from app.core.subscription_access import (
    get_user_subscription_tier,
    normalize_subscription_tier,
    user_can_access_tier,
)
from app.schemas.question import JEEMTestOut, QuestionDetailOut, QuestionSetOut
from app.services.question_service import (
    check_chapter_exists,
    get_jeem_test,
    get_jeea_test,
    get_neet_test,
    get_set_test,
    get_question_by_id,
    get_mcq_set,
    get_questions_by_uuids,
)
from app.services.supabase_client import sb_select

router = APIRouter(prefix="/questions", tags=["questions"])
logger = logging.getLogger(__name__)


def _set_dynamic_cache_headers(response: Response) -> None:
    """
    Prevent browsers/CDNs from reusing a cached free-tier response for an
    authenticated user. The questions payload changes based on Authorization.
    """
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Authorization"


async def _get_user_tier(user: Optional[TokenPayload]) -> Optional[str]:
    """Return the user's canonical subscription tier or None for unauthenticated/free users."""
    if not user:
        return None
    return await get_user_subscription_tier(
        user.sub,
        jwt_payload=getattr(user, "raw_payload", None),
    )


async def _get_set_tier(group_id: Optional[str]) -> Optional[str]:
    """
    Return the required tier for a question set or test (lowercased), or None when
    there is no restriction (any subscribed user may access).
    Checks question_set first, falls back to tests.
    """
    if not group_id:
        return None
    rows = await sb_select(
        "question_set",
        {"set_id": f"eq.{group_id}"},
        select_cols="tier",
        limit=1,
    )
    if rows:
        return normalize_subscription_tier(rows[0].get("tier"))
    rows = await sb_select(
        "tests",
        {"testID": f"eq.{group_id}"},
        select_cols="tier",
        limit=1,
    )
    if rows:
        return normalize_subscription_tier(rows[0].get("tier"))
    return None


def _user_can_access_set(user_tier: Optional[str], set_tier: Optional[str]) -> bool:
    """
    True when the user may access the full question set (not limited to the free preview).

    Rules — tier values come from the DB, no names hardcoded here:
      set_tier == 'free' → everyone passes, no subscription needed
      set_tier is None   → any subscribed user (non-null user_tier) passes
      set_tier is set    → user_tier must exactly equal set_tier
    """
    return user_can_access_tier(user_tier, set_tier)


@router.get("", response_model=QuestionSetOut)
async def list_questions(
    response: Response,
    setId: Optional[str] = Query(None, description="Practice set identifier — matched against used_in[]"),
    testId: Optional[str] = Query(None, description="Mock test identifier — matched against used_in[]"),
    subject: Optional[str] = Query(None),
    chapterCode: Optional[str] = Query(None),
    chapterCodes: Optional[str] = Query(None, description="Comma-separated chapter codes for section-level fetch"),
    uuids: Optional[str] = Query(None, description="Comma-separated question UUIDs for revision/bookmark fetch"),
    check: Optional[int] = Query(None, description="Set to 1 for existence-only check"),
    db: AsyncSession = Depends(get_db),
    user: Optional[TokenPayload] = Depends(get_optional_user),
):
    """
    Fetches a question set from Postgres.
    Replaces the 342-line `api/questions.js` serverless function.
    Free users get the first FREE_QUESTION_LIMIT questions; paid users get all, shuffled.

    Pass either setId or testId — both are matched 1-to-1 against the used_in TEXT[] column.
    Use chapterCodes (comma-separated) to filter by multiple chapters (e.g. section-level practice).
    Use uuids (comma-separated) to fetch specific questions by UUID — used by the Revision Box feature.
    """
    _set_dynamic_cache_headers(response)

    # UUID-based fetch for Revision Box — bypass set/chapter logic entirely
    if uuids:
        uuid_list = [u.strip() for u in uuids.split(",") if u.strip()]
        return await get_questions_by_uuids(db, uuids=uuid_list)

    # Chapter existence check (lightweight, no full data fetch)
    if check == 1:
        if not chapterCode or not subject:
            raise HTTPException(status_code=400, detail="chapterCode and subject required for existence check")
        exists = await check_chapter_exists(db, chapter_code=chapterCode, subject=subject)
        if exists:
            return {"questions": [], "solutions": {}, "totalCount": 0, "isPaid": False, "exists": True}
        raise HTTPException(status_code=404, detail="Chapter not found")

    chapter_codes_list = [c.strip() for c in chapterCodes.split(',')] if chapterCodes else None

    if not setId and not testId and not chapterCode and not chapter_codes_list:
        raise HTTPException(status_code=400, detail="Provide setId, testId, chapterCode, or chapterCodes")

    group_id = setId or testId
    # Fetch user tier and set tier in parallel — no extra latency vs. the old single lookup
    user_tier, set_tier = await asyncio.gather(
        _get_user_tier(user),
        _get_set_tier(group_id),
    )
    paid = _user_can_access_set(user_tier, set_tier)
    logger.warning(
        "Question entitlement resolved user_id=%s group_id=%s user_tier=%s set_tier=%s paid=%s",
        getattr(user, "sub", None),
        group_id,
        user_tier,
        set_tier,
        paid,
    )
    # Chapter-only queries (no setId) return MCQ div1 questions only
    chapter_only = bool((chapterCode or chapter_codes_list) and not setId and not testId)

    result = await get_mcq_set(
        db,
        group_id=group_id,
        subject=subject,
        chapter_code=chapterCode,
        chapter_codes=chapter_codes_list,
        is_paid=paid,
        div1_only=chapter_only,
    )
    return result


_SUPPORTED_OUTPUT_TYPES = {"JEEM", "NEET", "SET", "JEEA"}


@router.get("/test/{test_id}", response_model=JEEMTestOut)
async def get_structured_test(
    test_id: str,
    output_type: str = Query(..., description="Output format: JEEM | NEET | SET | JEEA"),
    title: Optional[str] = Query(None, description="Override test title"),
    duration: Optional[int] = Query(None, description="Override duration in seconds (default 10800 for JEEM)"),
    include_solutions: bool = Query(False, description="Include solutions in response (for review mode only)"),
    db: AsyncSession = Depends(get_db),
    user: Optional[TokenPayload] = Depends(get_optional_user),
):
    """
    Return a fully structured test JSON for the given test_id and output_type.

    JEEM — JEE Main format:
      3 subjects (Physics / Chemistry / Mathematics), each with:
        Section A: 20 MCQ questions (div1), +4 / -1
        Section B:  5 Integer questions (div2), +4 / 0
      Total: 75 questions, 300 marks, 3 hours

    The response shape matches the Test interface in client/src/utils/testData.ts
    so you can store the endpoint URL directly as tests.url in Supabase — no
    frontend or score-service changes required.

    Example Supabase tests.url value:
        https://backend.prepaired.site/api/v1/questions/test/jeem-mock-01?output_type=JEEM
    """
    if output_type not in _SUPPORTED_OUTPUT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported output_type '{output_type}'. Supported: {sorted(_SUPPORTED_OUTPUT_TYPES)}",
        )

    if output_type == "JEEM":
        return await get_jeem_test(
            db,
            test_id=test_id,
            test_title=title or "JEE Main Mock Test",
            duration=duration or 10800,
            include_solutions=include_solutions,
        )

    if output_type == "NEET":
        return await get_neet_test(
            db,
            test_id=test_id,
            test_title=title or "NEET Mock Test",
            duration=duration or 12000,
            include_solutions=include_solutions,
        )

    if output_type == "SET":
        return await get_set_test(
            db,
            test_id=test_id,
            test_title=title or "Practice Set",
            duration=duration or 3600,
            include_solutions=include_solutions,
        )

    if output_type == "JEEA":
        try:
            return await get_jeea_test(
                db,
                test_id=test_id,
                test_title=title or "JEE Advanced Mock Test",
                duration=duration or 10800,
                include_solutions=include_solutions,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


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
