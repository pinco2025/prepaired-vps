"""
Score calculation endpoints — replaces Render backend.

POST /api/v1/scores/{student_test_id}/calculate
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.deps import get_current_user
from app.core.security import TokenPayload
from app.services.score_service import score_service
from app.services.supabase_client import sb_select, SupabaseError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scores", tags=["scores"])


class CalculateScoreIn(BaseModel):
    answers: Dict[str, Any] | None = None


@router.post("/{student_test_id}/calculate", status_code=status.HTTP_200_OK)
async def calculate_score(
    student_test_id: str,
    force: bool = Query(False, description="Re-compute even when result_url is already set"),
    body: CalculateScoreIn | None = None,
    user: TokenPayload = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Calculate and persist the score for a submitted test.
    Pass ?force=true to recompute even if a result_url already exists.
    """
    try:
        # 1. Fetch student_tests row
        student_tests = await sb_select("student_tests", {"id": f"eq.{student_test_id}"})
        if not student_tests:
            raise HTTPException(status_code=404, detail="Student test not found")
        student_test = student_tests[0]

        # 2. Verify user ownership
        if student_test.get("user_id") != user.sub:
            raise HTTPException(status_code=403, detail="Not authorized to access this test")

        # 3. Return existing if already calculated (unless force recompute requested)
        existing_result_url = student_test.get("result_url")
        if existing_result_url and not force:
            logger.info("Score already calculated for %s. Returning existing URL.", student_test_id)
            return {"student_test_id": student_test_id, "github_url": existing_result_url}

        test_id = student_test.get("test_id")
        answers = (body.answers if body and body.answers else student_test.get("answers")) or {}

        if not test_id:
            raise HTTPException(status_code=400, detail="Test ID missing in student test record")
        if not answers:
            raise HTTPException(status_code=400, detail="No answers found — test has not been submitted")

        # 4. Run the full scoring pipeline
        try:
            github_url = await score_service.compute_and_persist_score(student_test_id, test_id, answers)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("Scoring pipeline failed for %s: %s", student_test_id, e)
            raise HTTPException(status_code=500, detail=f"Scoring pipeline error: {e}")

        return {"student_test_id": student_test_id, "github_url": github_url}

    except HTTPException:
        raise
    except SupabaseError as e:
        logger.error("Supabase error: %s", e)
        raise HTTPException(status_code=500, detail="Database integration error")
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        raise HTTPException(status_code=500, detail="An unexpected error occurred")
