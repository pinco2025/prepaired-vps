"""Pydantic schemas for test sessions (student_tests table via Supabase REST)."""

from typing import Any, Dict, List, Optional

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
    exam: Optional[str] = None
    type: Optional[str] = None


class SubmissionSummary(BaseModel):
    id: str
    test_id: str
    result_url: Optional[str] = None
    submitted_at: Optional[str] = None


class StudentTestByIdOut(BaseModel):
    id: str
    test_id: str


class TestsByPrefixOut(BaseModel):
    tests: List[Dict[str, Any]]
    submissions: List[SubmissionSummary]


class GenerateTestIn(BaseModel):
    exam: str  # "JEEM" — only supported value in v1


class GenerateTestOut(BaseModel):
    test_id: str
    exam: str
