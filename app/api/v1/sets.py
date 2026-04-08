"""
Practice set session endpoints — replaces setService.ts Supabase calls.
Critically: closeSessionOnUnload now uses keepalive fetch to this backend
instead of embedding Supabase credentials in the frontend bundle.

POST   /api/v1/sets/{setId}/start
GET    /api/v1/sets/{setId}/resume
PATCH  /api/v1/sets/{sessionId}/answers
PATCH  /api/v1/sets/{sessionId}/time
POST   /api/v1/sets/{sessionId}/close    ← keepalive target for onunload
"""

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from app.core.deps import get_current_user
from app.core.security import TokenPayload
from app.schemas.set import SetResumeOut, StartSetOut, UpdateAnswersIn, UpdateTimeIn
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


@router.post("/{session_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    session_id: str,
    user: TokenPayload = Depends(get_current_user),
):
    """
    Keepalive-compatible endpoint called from `closeSessionOnUnload`.
    Frontend uses `fetch(..., { keepalive: true })` so this fires even on tab close.
    """
    await set_service.close_session(session_id)
