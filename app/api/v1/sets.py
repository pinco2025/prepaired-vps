"""
Practice set session endpoints — replaces setService.ts Supabase calls.

POST   /api/v1/sets/{setId}/start
GET    /api/v1/sets/{setId}/resume
PATCH  /api/v1/sets/{sessionId}/answers
PATCH  /api/v1/sets/{sessionId}/time
POST   /api/v1/sets/{sessionId}/submit   ← marks submitted, returns score breakdown
POST   /api/v1/sets/{sessionId}/close    ← DEPRECATED: kept for admin tooling only
"""

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.security import TokenPayload
from app.schemas.set import (
    SetResumeOut,
    SetSubmitOut,
    StartSetOut,
    SubmitSetIn,
    UpdateAnswersIn,
    UpdateTimeIn,
)
from app.services import set_service

router = APIRouter(prefix="/sets", tags=["sets"])


@router.post("/{set_id}/start", response_model=StartSetOut)
async def start_set(
    set_id: str,
    questions_url: Optional[str] = Body(default=None, embed=True),
    user: TokenPayload = Depends(get_current_user),
):
    return await set_service.create_session(set_id, user.sub, questions_url=questions_url)


@router.get("/{set_id}/resume", response_model=Optional[SetResumeOut])
async def resume_set(
    set_id: str,
    user: TokenPayload = Depends(get_current_user),
):
    return await set_service.get_latest_session(set_id, user.sub)


@router.patch("/{session_id}/answers", status_code=status.HTTP_204_NO_CONTENT)
async def update_answers(
    session_id: str,
    body: UpdateAnswersIn,
    user: TokenPayload = Depends(get_current_user),
):
    await set_service.update_answers(session_id, body.answers)


@router.patch("/{session_id}/time", status_code=status.HTTP_204_NO_CONTENT)
async def update_time(
    session_id: str,
    body: UpdateTimeIn,
    user: TokenPayload = Depends(get_current_user),
):
    await set_service.update_time(session_id, body.time_elapsed)


@router.post("/{session_id}/submit", response_model=SetSubmitOut)
async def submit_set(
    session_id: str,
    body: SubmitSetIn,
    user: TokenPayload = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Finalise a practice set session. Scores the answers against Postgres question keys
    and returns a simple breakdown: correct / incorrect / unattempted / accuracy.
    No marks, no section breakdown — sets are pure practice.
    """
    return await set_service.submit_session(db, session_id, user.sub, body.answers)


@router.post("/{session_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    session_id: str,
    user: TokenPayload = Depends(get_current_user),
):
    """
    DEPRECATED — sessions are now persistent until submit.
    Kept for admin tooling. Frontend no longer calls this endpoint.
    """
    await set_service.close_session(session_id)
