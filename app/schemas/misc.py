"""Pydantic schemas for the remaining CRUD endpoints."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# ── Predictor ──────────────────────────────────────────────────────────────────

class PredictorIn(BaseModel):
    data: Dict[str, Any]


class PredictorOut(BaseModel):
    id: str
    user_id: str
    data: Dict[str, Any]
    created_at: Optional[str] = None


# ── Analytics ──────────────────────────────────────────────────────────────────

class UserAnalyticsOut(BaseModel):
    """Flexible — shape TBD as analytics logic is ported."""
    data: Dict[str, Any]


# ── Diagnostic assessment ──────────────────────────────────────────────────────

class DiagnosticAssessmentIn(BaseModel):
    chapter_scores: Dict[str, float]         # {chapter_code: score}
    subject: Optional[str] = None


class DiagnosticAssessmentOut(BaseModel):
    id: str
    user_id: str
    chapter_scores: Dict[str, float]
    subject: Optional[str] = None
    created_at: Optional[str] = None


# ── Diagnostic quiz ────────────────────────────────────────────────────────────

class DiagnosticQuizSubmitIn(BaseModel):
    answers: Dict[str, str]                  # {question_uuid: "A"|"B"|"C"|"D"}


class DiagnosticQuizResultOut(BaseModel):
    chapter_assessments: Dict[str, float]


# ── Question request ───────────────────────────────────────────────────────────

class QuestionRequestIn(BaseModel):
    subject: Optional[str] = None
    chapter: Optional[str] = None
    details: str = ""
