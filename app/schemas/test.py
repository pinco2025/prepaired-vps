"""Pydantic schemas for test sessions (student_tests table via Supabase REST)."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, model_validator


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


# LEGACY — remove once prepaired-web migrates to /tests/visible
class TestsAndSubmissionsOut(BaseModel):
    tests: List[Dict[str, Any]]
    submissions: List[SubmissionSummary]


class StudentTestByIdOut(BaseModel):
    id: str
    test_id: str


class TestsByPrefixOut(BaseModel):
    tests: List[Dict[str, Any]]
    submissions: List[SubmissionSummary]


class GenerateTestIn(BaseModel):
    exam: str  # "JEEM" — only supported value in v1
    mode: Literal["full", "custom"] = "full"
    subject: Optional[str] = None       # required when mode=custom
    chapters: Optional[List[str]] = None  # chapter codes; min 4 when mode=custom

    @model_validator(mode="after")
    def _validate_custom(self) -> "GenerateTestIn":
        if self.mode == "custom":
            if not self.subject:
                raise ValueError("subject is required when mode=custom")
            if not self.chapters or len(self.chapters) < 4:
                raise ValueError("at least 4 chapter codes are required when mode=custom")
        return self


class GenerateTestOut(BaseModel):
    test_id: str
    exam: str


class GenerationQuotaOut(BaseModel):
    used: int
    limit: Optional[int]      # None = unlimited
    resets_at: Optional[str]  # ISO-8601 UTC datetime string
    tier: str
