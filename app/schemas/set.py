"""Pydantic schemas for practice set sessions (student_sets table)."""

from typing import Any, Dict, Optional

from pydantic import BaseModel


class StartSetOut(BaseModel):
    id: str


class SetResumeOut(BaseModel):
    id: str
    answers: Dict[str, Any]


class UpdateAnswersIn(BaseModel):
    answers: Dict[str, Any]


class UpdateTimeIn(BaseModel):
    time_elapsed: int   # seconds; -1 = closed (legacy, kept for admin tooling)


class SubmitSetIn(BaseModel):
    answers: Dict[str, Any]   # final answer dict (in case last save raced)


class SetSubmitOut(BaseModel):
    session_id: str
    total: int
    correct: int
    incorrect: int
    unattempted: int
    accuracy: float           # 0.0–100.0, 1 decimal place
    submitted_at: str         # ISO 8601 timestamp
