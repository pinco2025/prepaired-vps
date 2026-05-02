"""
Generated test service — creation and JEEM output adapter for dynamic tests.

Responsibilities:
  1. create_generated_test: runs the generator, persists a row to dynamic_tests
     in Supabase (no stub in tests). Returns a "gen_"-prefixed test_id.
  2. fetch_jeem_from_resolution: takes an already-resolved TestResolution (from
     test_resolver.resolve_test) — no extra Supabase round-trip — fetches question
     rows from Postgres and shapes them into JEEMTestOut, identical in structure
     to the curated get_jeem_test output.

Routing (is_generated_test / fetch_jeem_from_manifest) is gone. All entry points
use test_resolver.resolve_test and pass the TestResolution here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import uuid6
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.blueprints import DivQuota, ExamBlueprint, SubjectBlueprint
from app.core.blueprints.jeem import BLUEPRINTS, JEEM_BLUEPRINT_V1
from app.core.subscription_access import get_user_subscription_tier, normalize_subscription_tier
from app.models.question import Question
from app.schemas.question import JEEMQuestionOut, JEEMTestOut, SolutionOut
from app.services import test_generator
from app.services.supabase_client import sb_insert, sb_select
from app.services.test_resolver import TestResolution

logger = logging.getLogger(__name__)

from app.services.question_service import (
    _JEEM_DIV_CONFIG,
    _JEEM_SECTIONS,
    _orm_to_jeem_question_out,
    _orm_to_solution_out,
)


# ── Quota helpers ─────────────────────────────────────────────────────────────

_LITE_LIMIT = 2
_WINDOW_DAYS = 30

# Paid tiers that get unlimited generation — any tier not in this blocklist is unlimited
_BLOCKED_TIERS = {None, "free"}
_LITE_TIER = "lite"


async def get_generation_quota_state(user_id: str) -> Dict[str, Any]:
    """
    Returns {"used": int, "limit": int|None, "resets_at": str|None, "tier": str}.
    limit=None means unlimited; limit=0 means fully blocked (free tier).
    """
    raw_tier = await get_user_subscription_tier(user_id)
    tier = normalize_subscription_tier(raw_tier) or "free"

    if tier in _BLOCKED_TIERS:
        return {"used": 0, "limit": 0, "resets_at": None, "tier": tier}

    if tier != _LITE_TIER:
        return {"used": 0, "limit": None, "resets_at": None, "tier": tier}

    # Lite: 2 per rolling 30-day window
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=_WINDOW_DAYS)

    rows = await sb_select(
        "dynamic_tests",
        {
            "created_by": f"eq.{user_id}",
            "created_at": f"gte.{window_start.isoformat()}",
        },
        select_cols="created_at",
        order="created_at.asc",
        limit=_LITE_LIMIT + 1,
    )

    used = len(rows)
    resets_at: Optional[str] = None
    if rows:
        earliest_str = rows[0]["created_at"]
        try:
            earliest_dt = datetime.fromisoformat(earliest_str.replace("Z", "+00:00"))
            resets_at = (earliest_dt + timedelta(days=_WINDOW_DAYS)).isoformat()
        except (ValueError, AttributeError):
            pass

    return {"used": used, "limit": _LITE_LIMIT, "resets_at": resets_at, "tier": tier}


def _build_custom_single_subject_blueprint(subject: str, chapters: List[str]) -> ExamBlueprint:
    """
    Build a single-subject JEEM-style blueprint (20 MCQ + 5 Integer = 25 Q, 100 marks).
    Chapter weights are set uniformly to 1.0 for all provided chapters; min_chapters is
    set to the user's selection size to avoid spurious warnings.
    """
    n = len(chapters)
    blueprint = ExamBlueprint(
        exam="JEEM",
        version="custom-v1",
        duration_seconds=3600,
        title=f"Generated {subject.capitalize()} Test",
        total_marks=100,
        min_pool_per_chapter_div=1,
        subjects=(
            SubjectBlueprint(
                subject=subject.lower(),
                quotas=(
                    DivQuota(div="div1", count=20, min_per_chapter=1, min_chapters=min(n, 8)),
                    DivQuota(div="div2", count=5,  min_per_chapter=1, min_chapters=min(n, 4)),
                ),
                chapter_weights={ch: 1.0 for ch in chapters},
            ),
        ),
    )
    return blueprint


# ── Creation ──────────────────────────────────────────────────────────────────

async def create_generated_test(
    db: AsyncSession,
    *,
    exam: str,
    user_id: Optional[str] = None,
    subject: Optional[str] = None,
    chapters: Optional[List[str]] = None,
) -> str:
    """
    Generate a test from the globally_open pool and persist it to dynamic_tests.
    Returns a "gen_"-prefixed test_id. No stub is written to tests.

    When subject + chapters are provided (custom mode), a single-subject
    25-question test is generated from those chapters only.
    """
    # ── Quota / tier enforcement ──────────────────────────────────────────────
    if user_id:
        raw_tier = await get_user_subscription_tier(user_id)
        tier = normalize_subscription_tier(raw_tier) or "free"
        if tier in _BLOCKED_TIERS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Test generation requires a paid subscription.",
            )
        if tier == _LITE_TIER:
            quota = await get_generation_quota_state(user_id)
            if quota["limit"] is not None and quota["used"] >= quota["limit"]:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={"message": "Monthly generation quota reached.", "resets_at": quota["resets_at"]},
                )

    # ── Blueprint selection ───────────────────────────────────────────────────
    chapter_whitelists: Optional[Dict[str, List[str]]] = None

    if subject and chapters:
        blueprint = _build_custom_single_subject_blueprint(subject, chapters)
        chapter_whitelists = {subject.lower(): chapters}
        exam_upper = "JEEM"
    else:
        blueprint = BLUEPRINTS.get(exam.upper())
        if blueprint is None:
            raise ValueError(f"No blueprint registered for exam '{exam}'")
        exam_upper = exam.upper()

    manifest_obj = await test_generator.generate(db, blueprint, chapter_whitelists=chapter_whitelists)

    # Build manifest dict preserving section render order from the generator
    seen_subjects: Dict[str, List[Dict[str, Any]]] = {}
    for sec in manifest_obj.sections:
        seen_subjects.setdefault(sec.subject, []).append({
            "div": sec.div,
            "section_name": sec.section_name,
            "question_ids": sec.question_ids,
        })

    manifest_dict: Dict[str, Any] = {
        "subjects": [
            {"subject": subj, "sections": sections}
            for subj, sections in seen_subjects.items()
        ]
    }

    test_id = f"gen_{uuid6.uuid7()}"

    await sb_insert("dynamic_tests", {
        "id": test_id,
        "exam": exam_upper,
        "blueprint_version": manifest_obj.blueprint_version,
        "title": blueprint.title,
        "duration": blueprint.duration_seconds,
        "total_marks": blueprint.total_marks,
        "seed": manifest_obj.seed,
        "created_by": user_id,
        "manifest": manifest_dict,
    })

    if manifest_obj.warnings:
        logger.warning(
            "create_generated_test: test_id=%s generated with %d warning(s): %s",
            test_id, len(manifest_obj.warnings), manifest_obj.warnings,
        )

    return test_id


# ── JEEM output adapter ───────────────────────────────────────────────────────

async def fetch_jeem_from_resolution(
    db: AsyncSession,
    resolution: TestResolution,
    *,
    test_title: Optional[str] = None,
    duration: Optional[int] = None,
    include_solutions: bool = False,
) -> JEEMTestOut:
    """
    Build a JEEMTestOut from an already-resolved generated test.

    The manifest is taken directly from resolution.raw — no additional Supabase
    round-trip. Postgres is hit once to fetch question rows by UUID list.
    Output is structurally identical to get_jeem_test (curated path).
    """
    manifest = resolution.raw.get("manifest", {})
    title = test_title or resolution.meta.title
    dur = duration or resolution.meta.duration

    section_index: Dict[str, tuple[str, str]] = {}  # qid -> (section_name, div)
    ordered_ids: List[str] = []
    for subj_entry in manifest.get("subjects", []):
        for sec in subj_entry.get("sections", []):
            section_name = sec["section_name"]
            div = sec["div"]
            for qid in sec["question_ids"]:
                section_index[qid] = (section_name, div)
                ordered_ids.append(qid)

    if not ordered_ids:
        return JEEMTestOut(
            testId=resolution.meta.id,
            title=title,
            duration=dur,
            totalMarks=0,
            sections=_JEEM_SECTIONS,
            questions=[],
            solutions={},
        )

    stmt = select(Question).where(Question.id.in_(ordered_ids))
    if include_solutions:
        stmt = stmt.options(selectinload(Question.solution))
    result = await db.execute(stmt)
    questions_by_id: Dict[str, Question] = {q.id: q for q in result.scalars().all()}

    questions_out: List[JEEMQuestionOut] = []
    total_marks = 0
    for qid in ordered_ids:
        q = questions_by_id.get(qid)
        if q is None:
            logger.warning("fetch_jeem_from_resolution: question_id=%s missing — skipped", qid)
            continue
        section_name, div = section_index[qid]
        spec = _JEEM_DIV_CONFIG.get(div, _JEEM_DIV_CONFIG["div1"])
        questions_out.append(_orm_to_jeem_question_out(q, section_name, spec.q_type, spec.marks))
        total_marks += spec.marks

    solutions_out: Dict[str, SolutionOut] = {}
    if include_solutions:
        for qid in ordered_ids:
            q = questions_by_id.get(qid)
            if q:
                sol = _orm_to_solution_out(q.solution)
                if sol:
                    solutions_out[q.legacy_id or q.id] = sol

    return JEEMTestOut(
        testId=resolution.meta.id,
        title=title,
        duration=dur,
        totalMarks=total_marks,
        sections=_JEEM_SECTIONS,
        questions=questions_out,
        solutions=solutions_out,
    )
