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
    time_elapsed: int   # seconds; -1 = closed
