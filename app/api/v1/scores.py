"""
Score calculation endpoints — replaces Render backend.

POST /api/v1/scores/{student_test_id}/calculate
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.deps import get_current_user
from app.core.security import TokenPayload
from app.services.score_service import score_service
from app.services.supabase_client import sb_select, sb_update, SupabaseError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scores", tags=["scores"])


class CalculateScoreIn(BaseModel):
    # Depending on frontend, it might pass answers or we can rely on DB. 
    # Legacy didn't need it, but scaffold had this, so we make it optional to support both.
    answers: Dict[str, Any] | None = None


@router.post("/{student_test_id}/calculate", status_code=status.HTTP_200_OK)
async def calculate_score(
    student_test_id: str,
    body: CalculateScoreIn | None = None,
    user: TokenPayload = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Calculate and persist the score for a submitted test.
    Ported from the legacy Render backend.
    """
    try:
        # 1. Fetch student_tests row
        student_tests = await sb_select("student_tests", {"id": f"eq.{student_test_id}"})
        if not student_tests:
            raise HTTPException(status_code=404, detail="Student test not found")
        student_test = student_tests[0]
        
        # 2. Verify user ownership
        if student_test.get("user_id") != user.sub: # sub is the user ID in Supabase JWT
            raise HTTPException(status_code=403, detail="Not authorized to access this test")
            
        # 3. Return existing if already calculated
        existing_result_url = student_test.get("result_url")
        if existing_result_url:
            logger.info(f"Score already calculated for {student_test_id}. Returning existing URL.")
            return {"student_test_id": student_test_id, "github_url": existing_result_url}
            
        test_id = student_test.get("test_id")
        # Use answers from body if provided, else from DB
        answers = (body.answers if body and body.answers else student_test.get("answers")) or {}

        if not test_id:
            raise HTTPException(status_code=400, detail="Test ID missing in student test record")

        if not answers:
            raise HTTPException(status_code=400, detail="No answers found — test has not been submitted")

        # 4. Fetch section_config from Supabase tests table (marking scheme)
        tests = await sb_select("tests", {"testID": f"eq.{test_id}"})
        if not tests:
            raise HTTPException(status_code=404, detail="Test definition not found")
        section_config: Dict[str, Any] = tests[0].get("section_config") or {}
        if not section_config:
            raise HTTPException(status_code=400, detail="Test section_config not configured — populate tests.section_config before scoring")

        # 5. Fetch ALL questions for this test from Postgres.
        #    We use the used_in[] column to get every question in the test,
        #    not just the ones the student answered — so unattempted questions
        #    appear in attempt_comparison with status "Unattempted".
        answer_keys_set = set(answers.keys())

        async with AsyncSessionLocal() as db:
            rows = await db.execute(
                text("""
                    SELECT
                        id::text                            AS db_id,
                        legacy_id,
                        answer,
                        chapter,
                        type                                AS question_type,
                        subject,
                        source_info->>'section_type'        AS section_type,
                        source_info->>'difficulty'          AS difficulty,
                        flags->>'scary'                     AS scary
                    FROM questions
                    WHERE :test_id = ANY(used_in)
                      AND verification_status = 'verified'
                """),
                {"test_id": test_id},
            )
            q_rows = rows.fetchall()

        # 6. Reconstruct ppt_data-compatible dict — score_service.calculate_score unchanged
        #    Section key = "{subject}-{section_type}" e.g. "Physics-div1"
        sections_map: Dict[str, Any] = {}
        questions_out = []
        for r in q_rows:
            # Use whichever key the student actually submitted answers under
            answer_key = r.legacy_id if (r.legacy_id and r.legacy_id in answer_keys_set) else r.db_id

            section_key = (
                f"{r.subject.lower()}-{r.section_type}"
                if r.subject and r.section_type
                else (r.section_type or "unknown")
            )
            sec = section_config.get(section_key, {})
            section_name = sec.get("name", section_key)

            if section_name not in sections_map:
                sections_map[section_name] = {
                    "name": section_name,
                    "marksPerQuestion": float(sec.get("pos", 0)),
                    "negativeMarksPerQuestion": float(sec.get("neg", 0)),
                }

            questions_out.append({
                "uuid": answer_key,     # must match the key in answers dict
                "id": answer_key,       # kept for parity with test JSON shape
                "section": section_name,
                "correctAnswer": r.answer,
                "chapterCode": r.chapter,
                "questionType": r.question_type,
                "difficulty": r.difficulty,
                "scary": r.scary,
            })

        ppt_data = {
            "sections": list(sections_map.values()),
            "questions": questions_out,
        }

        # 7. Calculate scores — algorithm unchanged
        try:
            result = score_service.calculate_score(ppt_data, answers)
        except Exception as e:
            logger.error(f"Error calculating score: {e}")
            raise HTTPException(status_code=500, detail="Error calculating score")
            
        # 8. Push to GitHub
        try:
            filename = f"{student_test_id}.json"
            github_url = await score_service.push_to_github(result, filename)
        except Exception as e:
            logger.error(f"Error pushing results to GitHub: {e}")
            raise HTTPException(status_code=502, detail=f"Error pushing results to GitHub: {str(e)}")
            
        # 9. Update student_tests with result_url
        try:
            await sb_update("student_tests", {"id": f"eq.{student_test_id}"}, {"result_url": github_url})
        except SupabaseError as e:
            logger.error(f"Error updating student_tests with result URL: {e}")
            
        # Note: Trigger Analytics omitted here; wait until analytics_service is implemented
        # or implement a placeholder in a future phase.
        
        return {
            "student_test_id": student_test_id, 
            "github_url": github_url
        }

    except HTTPException:
        raise
    except SupabaseError as e:
        logger.error(f"Supabase error: {e}")
        raise HTTPException(status_code=500, detail="Database integration error")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")
