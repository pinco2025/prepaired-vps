"""
Pydantic schemas for the questions & solutions tables.
These are the *response* shapes sent to the frontend — they deliberately
match what the existing api/questions.js returns so the frontend works
without changes during migration.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Sub-schemas ─────────────────────────────────────────────────────────────────

class QuestionContent(BaseModel):
    """Mirrors the JSONB `question` column: {text, image_url}"""
    text: str = ""
    image_url: Optional[str] = None


class OptionContent(BaseModel):
    """Single option inside the JSONB `options` column."""
    text: str = ""
    image_url: Optional[str] = None


class SourceInfo(BaseModel):
    source_code: Optional[str] = None
    source_q_no: Optional[str] = None
    difficulty: Optional[str] = None          # "E" | "M" | "H"
    section_type: Optional[str] = None
    legacy_table: Optional[str] = None


class Flags(BaseModel):
    scary: bool = False
    calc: bool = False
    multi_concept: bool = False


class Stats(BaseModel):
    freq: int = 0


# ── Solution schema ─────────────────────────────────────────────────────────────

class SolutionOut(BaseModel):
    """Flattened solution sent alongside each question."""
    text: str = Field(default="", alias="explanation")
    image: Optional[str] = Field(default=None, alias="solution_image_url")

    model_config = {"populate_by_name": True}


# ── Question schemas ────────────────────────────────────────────────────────────

class QuestionOut(BaseModel):
    """
    Public-facing question shape.
    Matches the legacy `api/questions.js` output so frontend needs zero changes.
    """
    id: str                                    # UUID string (legacy_id fallback in service)
    uuid: str                                  # Same as id — kept for compat
    text: str                                  # question.text
    image: Optional[str] = None               # question.image_url
    options: List[Dict[str, Any]]             # [{id, text, image}, ...]
    correctAnswer: Optional[str] = None       # answer (exposed only when isPaid or after submit)
    chapterCode: Optional[str] = None
    year: Optional[int] = None
    type: Optional[str] = None
    subject: Optional[str] = None
    flags: Optional[Flags] = None
    source_info: Optional[SourceInfo] = None
    paragraph_id: Optional[str] = None         # non-null for div5 questions once grouped


class QuestionSetOut(BaseModel):
    """Top-level response for GET /api/v1/questions"""
    questions: List[QuestionOut]
    solutions: Dict[str, SolutionOut]         # keyed by question UUID
    totalCount: int
    isPaid: bool


class QuestionDetailOut(BaseModel):
    """Full single-question response with embedded solution."""
    question: QuestionOut
    solution: Optional[SolutionOut] = None


# ── JEEM (JEE Main) structured test output ───────────────────────────────────
# Matches the Test interface in client/src/utils/testData.ts exactly so that
# fetchTestData() and score_service.calculate_score() both work without changes.

class JEEMSectionConfig(BaseModel):
    """One section row in the top-level sections[] array."""
    name: str
    marksPerQuestion: int
    negativeMarksPerQuestion: int


class JEEMQuestionTags(BaseModel):
    tag1: str = ""   # source_code (source material reference)
    tag2: str = ""   # chapter code — used by score_service for chapter breakdown
    tag3: str = ""
    tag4: str = ""   # source_q_no
    type: str = ""   # "MCQ" | "Integer"
    year: str = ""


class JEEMQuestionOut(BaseModel):
    """
    Per-question shape inside the JEEM test response.
    Extends the base question fields with fields the score service reads.
    """
    id: str
    uuid: str
    text: str
    image: Optional[str] = None
    options: List[Dict[str, Any]]
    correctAnswer: Optional[str] = None
    marks: int
    section: str                           # e.g. "Physics - Section A"
    chapterCode: Optional[str] = None
    difficulty: Optional[str] = None       # "E" | "M" | "H"
    questionType: Optional[str] = None     # "MCQ" | "Integer" | "MultiCorrect" — read by score_service
    tags: JEEMQuestionTags = JEEMQuestionTags()


class JEEMTestOut(BaseModel):
    """
    Top-level JEEM test response — mirrors the Test interface in testData.ts.
    Served at GET /api/v1/questions/test/{testId}?output_type=JEEM.
    Store this endpoint URL as tests.url in Supabase for seamless integration.
    """
    testId: str
    title: str
    duration: int                          # seconds — 3 hours = 10800
    totalMarks: int                        # 300 for standard JEEM
    sections: List[JEEMSectionConfig]
    questions: List[JEEMQuestionOut]
