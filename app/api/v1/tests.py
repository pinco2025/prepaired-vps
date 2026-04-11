"""
Test session endpoints — replaces testService.ts Supabase calls.

POST   /api/v1/tests/{testId}/start
POST   /api/v1/tests/{testId}/save
POST   /api/v1/tests/{testId}/submit
GET    /api/v1/tests/submissions          — list tests + submissions for user
GET    /api/v1/tests/meta/{testId}        — 99ile data
GET    /api/v1/tests/attempts/{testId}    — user's past attempts
GET    /api/v1/tests/result/{submissionId}
"""

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from app.core.deps import get_current_user
from app.core.security import TokenPayload
from app.schemas.test import (
    AttemptOut,
    SaveAnswersIn,
    StartTestOut,
    StudentTestByIdOut,
    SubmitTestIn,
    SubmitTestOut,
    TestMetaOut,
    TestResultOut,
    TestsAndSubmissionsOut,
    TestsByPrefixOut,
)
from app.services import test_service

router = APIRouter(prefix="/tests", tags=["tests"])


# ── Static-path routes (must appear before /{param} routes) ──────────────────

@router.get("/submissions", response_model=TestsAndSubmissionsOut)
async def get_submissions(
    user: TokenPayload = Depends(get_current_user),
):
    """All tests + the current user's submitted attempts."""
    return await test_service.get_tests_and_submissions(user.sub)


@router.get("/by-prefix", response_model=TestsByPrefixOut)
async def get_by_prefix(
    prefix: str = Query(..., description="PostgREST ilike pattern, e.g. 'AIPT-%'"),
    user: TokenPayload = Depends(get_current_user),
):
    """Tests whose testID matches the given ilike pattern, plus user submissions."""
    return await test_service.get_tests_by_prefix(prefix, user.sub)


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
