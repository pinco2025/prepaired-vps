"""Pydantic schemas for feedback and question reports."""

from typing import List, Optional

from pydantic import BaseModel


# ── User feedback ──────────────────────────────────────────────────────────────

class SubmitFeedbackIn(BaseModel):
    aipt_rating: Optional[int] = None
    question_set_rating: Optional[int] = None
    ux_rating: Optional[int] = None
    remarks: str = ""


class FeedbackOut(BaseModel):
    id: str
    user_id: str
    aipt_rating: Optional[int] = None
    question_set_rating: Optional[int] = None
    ux_rating: Optional[int] = None
    remarks: Optional[str] = None
    submitted_at: Optional[str] = None


# ── Question reports ───────────────────────────────────────────────────────────

class ReportQuestionIn(BaseModel):
    question_id: str
    reported_parts: List[str]
    source_url: Optional[str] = "internal"


class QuestionReportOut(BaseModel):
    id: str
    question_uuid: str
    reported_parts: List[str]
    user_id: Optional[str] = None
    reported_at: Optional[str] = None
    is_resolved: bool = False
    source_url: Optional[str] = None


class UpdateReportIn(BaseModel):
    is_resolved: bool
