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
from typing import Any, Dict, List, Optional

import uuid6
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.blueprints.jeem import BLUEPRINTS
from app.models.question import Question
from app.schemas.question import JEEMQuestionOut, JEEMTestOut, SolutionOut
from app.services import test_generator
from app.services.supabase_client import sb_insert
from app.services.test_resolver import TestResolution

logger = logging.getLogger(__name__)

from app.services.question_service import (
    _JEEM_DIV_CONFIG,
    _JEEM_SECTIONS,
    _orm_to_jeem_question_out,
    _orm_to_solution_out,
)


# ── Creation ──────────────────────────────────────────────────────────────────

async def create_generated_test(
    db: AsyncSession,
    *,
    exam: str,
    user_id: Optional[str] = None,
) -> str:
    """
    Generate a test from the globally_open pool and persist it to dynamic_tests.
    Returns a "gen_"-prefixed test_id. No stub is written to tests.
    """
    blueprint = BLUEPRINTS.get(exam.upper())
    if blueprint is None:
        raise ValueError(f"No blueprint registered for exam '{exam}'")

    manifest_obj = await test_generator.generate(db, blueprint)

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
        "exam": manifest_obj.exam,
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
