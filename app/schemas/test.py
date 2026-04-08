"""Pydantic schemas for test sessions (student_tests table via Supabase REST)."""

from typing import Any, Dict, Optional

from pydantic import BaseModel


class StartTestOut(BaseModel):
    id: str
    started_at: str
    answers: Optional[Dict[str, str]] = None


class SaveAnswersIn(BaseModel):
    answers: Dict[str, str]


class SubmitTestIn(BaseModel):
    answers: Dict[str, str]


class SubmitTestOut(BaseModel):
    submission_id: str


class TestMetaOut(BaseModel):
    percentile_99: Optional[float] = None     # maps DB column "99ile"


class AttemptOut(BaseModel):
    id: str
    submitted_at: Optional[str] = None
    started_at: Optional[str] = None


class TestResultOut(BaseModel):
    id: str
    test_id: str
    submitted_at: Optional[str] = None
    started_at: Optional[str] = None
    result_url: Optional[str] = None
