"""
Question set configuration endpoints — replaces direct Supabase calls in
questionService.ts (getSetConfigs, getSetUrl).

GET  /api/v1/question-sets/configs          → all SET-type configs
GET  /api/v1/question-sets/{set_id}/url     → single set's source URL
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.services.supabase_client import sb_select

router = APIRouter(prefix="/question-sets", tags=["question-sets"])


class SetConfigOut(BaseModel):
    set_id: str
    url: Optional[str] = None
    tier: Optional[str] = None
    exam_types: Optional[Any] = None
    visibility: Optional[str] = None
    subjects: Optional[Any] = None


class SetUrlOut(BaseModel):
    url: Optional[str] = None


@router.get("/configs", response_model=List[SetConfigOut])
async def get_set_configs():
    """
    Returns all SET-type question set metadata.
    Public endpoint — no auth required (set configs are not user-specific).
    """
    rows = await sb_select(
        "question_set",
        {"type": "eq.SET"},
        select_cols="set_id,url,tier,exam_types,visibility,subjects",
    )
    return [SetConfigOut(**r) for r in rows]


@router.get("/{set_id}/url", response_model=SetUrlOut)
async def get_set_url(set_id: str):
    """
    Returns the source URL for a single set (used by NCERTLinePractice).
    Returns {url: null} when the set is not found rather than 404,
    matching the previous maybeSingle() behaviour.
    """
    rows = await sb_select(
        "question_set",
        {"set_id": f"eq.{set_id}"},
        select_cols="url",
        limit=1,
    )
    if not rows:
        return SetUrlOut(url=None)
    return SetUrlOut(url=rows[0].get("url"))
