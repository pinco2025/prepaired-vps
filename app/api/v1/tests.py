"""
Test endpoints.

Session lifecycle:
  POST   /api/v1/tests/{testId}/start
  POST   /api/v1/tests/{testId}/save
  POST   /api/v1/tests/{testId}/submit

Test listing (personalised, archive-free):
  GET    /api/v1/tests/visible            — server-derived audience from JWT

Metadata & history:
  GET    /api/v1/tests/meta/{testId}
  GET    /api/v1/tests/attempts/{testId}
  GET    /api/v1/tests/result/{submissionId}
  GET    /api/v1/tests/by-ids
  GET    /api/v1/tests/{testId}/detail
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user, get_optional_user
from app.core.security import TokenPayload
from app.schemas.test import (
    AttemptOut,
    GenerateTestIn,
    GenerateTestOut,
    SaveAnswersIn,
    StartTestOut,
    StudentTestByIdOut,
    SubmitTestIn,
    SubmitTestOut,
    TestMetaOut,
    TestResultOut,
    TestsByPrefixOut,
    TestsAndSubmissionsOut,
)
from app.services import generated_test_service, test_service, test_resolver

router = APIRouter(prefix="/tests", tags=["tests"])


# ── Static-path routes (must appear before /{param} routes) ──────────────────

@router.get("/visible", response_model=TestsByPrefixOut)
async def get_visible(
    prefix: Optional[str] = Query(
        default=None,
        description="Optional testID ilike pattern to narrow results, e.g. 'AIPT%'",
    ),
    user: TokenPayload = Depends(get_current_user),
):
    """
    Personalised, archive-free test list for the authenticated user.

    The server derives the user's audience tags from their onboarding profile
    (exam_type, user_level, onboarding_prefs) — no exam/level params needed.
    Tests with type='archive' are always excluded.
    """
    return await test_service.get_visible_tests(user.sub, prefix=prefix)


# ── LEGACY routes — remove once prepaired-web migrates to /tests/visible ────

@router.get("/submissions", response_model=TestsAndSubmissionsOut)
async def legacy_get_submissions(
    user: TokenPayload = Depends(get_current_user),
):
    """LEGACY: used by prepaired-web Tests.tsx. Migrate callers to GET /tests/visible."""
    return await test_service.get_tests_and_submissions(user.sub)


@router.get("/by-prefix", response_model=TestsByPrefixOut)
async def legacy_get_by_prefix(
    prefix: str = Query(..., description="PostgREST ilike pattern, e.g. 'APYQ-%'"),
    user: Optional[TokenPayload] = Depends(get_optional_user),
):
    """LEGACY: used by prepaired-web Pyq2026.tsx. Migrate callers to GET /tests/visible?prefix=..."""
    return await test_service.get_tests_by_prefix(prefix, user.sub if user else None)


@router.get("/by-exam", response_model=TestsByPrefixOut)
async def legacy_get_by_exam(
    exam: str = Query(..., description="Exam type, e.g. 'JEE', 'NEET'"),
    user: Optional[TokenPayload] = Depends(get_optional_user),
):
    """LEGACY: used by prepaired-web AIPTPage.tsx. Migrate callers to GET /tests/visible."""
    return await test_service.get_tests_by_exam(exam.upper(), user.sub if user else None)


# ── End legacy ────────────────────────────────────────────────────────────────

@router.post("/generate", response_model=GenerateTestOut)
async def generate_test(
    body: GenerateTestIn,
    db: AsyncSession = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
):
    """
    Dynamically assemble a test paper from the globally_open question pool.

    Currently supports exam="JEEM" (75 questions: 20 MCQ + 5 Integer per subject).
    Returns a test_id that can be used with all existing test endpoints:
      GET  /api/v1/questions/test/{test_id}?output_type=JEEM
      POST /api/v1/tests/{test_id}/start
      POST /api/v1/tests/{student_test_id}/save
      POST /api/v1/tests/{student_test_id}/submit
    """
    supported = {"JEEM"}
    if body.exam.upper() not in supported:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported exam '{body.exam}'. Supported: {sorted(supported)}",
        )
    try:
        test_id = await generated_test_service.create_generated_test(
            db, exam=body.exam.upper(), user_id=user.sub,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return GenerateTestOut(test_id=test_id, exam=body.exam.upper())


@router.get("/by-ids", response_model=list[StudentTestByIdOut])
async def get_by_ids(
    ids: str = Query(..., description="Comma-separated student_test UUIDs"),
    user: TokenPayload = Depends(get_current_user),
):
    """Batch-fetch student_test records (id + test_id) by a comma-separated list of IDs."""
    id_list = [i.strip() for i in ids.split(",") if i.strip()]
    return await test_service.get_student_tests_by_ids(id_list)


# ── Parameterised routes ──────────────────────────────────────────────────────

@router.post("/{test_id}/start", response_model=StartTestOut)
async def start_test(
    test_id: str,
    is_reattempt: bool = Body(default=False, embed=True),
    user: TokenPayload = Depends(get_current_user),
):
    result = await test_service.start_or_resume(
        test_id, user.sub, is_reattempt=is_reattempt
    )
    if not result:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to start test")
    return result


@router.post("/{student_test_id}/save", status_code=status.HTTP_204_NO_CONTENT)
async def save_answers(
    student_test_id: str,
    body: SaveAnswersIn,
    user: TokenPayload = Depends(get_current_user),
):
    await test_service.save_answers(student_test_id, body.answers)


@router.post("/{student_test_id}/submit", response_model=SubmitTestOut)
async def submit_test(
    student_test_id: str,
    body: SubmitTestIn,
    user: TokenPayload = Depends(get_current_user),
):
    return await test_service.submit_test(student_test_id, body.answers)


@router.get("/attempts/{test_id}", response_model=list[AttemptOut])
async def get_attempts(
    test_id: str,
    user: TokenPayload = Depends(get_current_user),
):
    return await test_service.get_attempts(test_id, user.sub)


@router.get("/result/{submission_id}", response_model=TestResultOut)
async def get_result(
    submission_id: str,
    user: TokenPayload = Depends(get_current_user),
):
    result = await test_service.get_result(submission_id)
    if not result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submission not found")
    return result


@router.get("/meta/{test_id}", response_model=TestMetaOut)
async def get_meta(
    test_id: str,
    user: TokenPayload = Depends(get_current_user),
):
    meta = await test_service.get_meta(test_id)
    if not meta:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test metadata not found")
    return meta


@router.get("/{test_id}/detail", response_model=Dict[str, Any])
async def get_test_detail(
    test_id: str,
    user: TokenPayload = Depends(get_current_user),
):
    """Fetch full test row from dynamic_tests (generated) or tests (curated) by testID."""
    resolution = await test_resolver.try_resolve_test(test_id)
    if not resolution:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test not found")
    return resolution.raw
