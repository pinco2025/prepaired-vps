"""
Test session service — thin wrapper over Supabase REST (student_tests / tests tables).
All business logic lives here so the router stays clean.
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

from app.schemas.test import (
    AttemptOut,
    StartTestOut,
    StudentTestByIdOut,
    SubmitTestOut,
    SubmissionSummary,
    TestMetaOut,
    TestResultOut,
    TestsByPrefixOut,
)
from app.services.supabase_client import sb_insert, sb_select, sb_update


# ── Visibility filter ─────────────────────────────────────────────────────────

def _build_visibility_filter(
    user_tags: List[str],
    exam_type: Optional[str] = None,
) -> Dict[str, str]:
    """
    Returns a PostgREST `and` filter param that:
      1. Excludes tests with type = 'archive' (including NULL-safe check).
      2. Scopes tests to the user's exam when exam_type is known:
         - tests where exam IS NULL or exam = 'Normal' are always included
         - tests whose exam matches the user's exam_type are included
         - additionally, tests matched by audience_tags overlap are included
         This means a JEE user never sees exam='NEET' rows (unless audience_tags
         explicitly target them, which in practice won't happen).
      3. Falls back to showing all non-archived tests for unonboarded users.

    Usage: merge the returned dict into the sb_select filters argument.
    """
    archive_clause = "or(type.is.null,type.neq.archive)"

    if exam_type in ("JEE", "NEET"):
        # Rows with no exam or exam=Normal are generic — always include them.
        # Rows whose exam matches the user's exam are included.
        # Rows matched via audience_tags overlap are also included (for newer rows).
        exam_clause = f"or(exam.is.null,exam.eq.Normal,exam.eq.{exam_type})"
        if user_tags:
            tags_csv = ",".join(user_tags)
            audience_clause = f"audience_tags.ov.{{{tags_csv}}}"
            scope_clause = f"or({exam_clause},{audience_clause})"
        else:
            scope_clause = exam_clause
        return {"and": f"({archive_clause},{scope_clause})"}

    # Unonboarded user — preserve prior behavior (show all non-archived tests).
    return {"and": f"({archive_clause})"}


# ── Visible test listing ──────────────────────────────────────────────────────

async def get_visible_tests(
    user_id: str,
    prefix: Optional[str] = None,
) -> TestsByPrefixOut:
    """
    Returns the personalised, archive-free list of tests for a user.

    Server derives audience tags from the user's onboarding profile so the
    client needs no exam/level params.  An optional `prefix` narrows by testID
    ilike pattern (e.g. 'AIPT%') for screens that show a specific series.
    """
    from app.core.audience import get_user_profile

    exam_type, user_tags = await get_user_profile(user_id)
    tests_filter = _build_visibility_filter(user_tags, exam_type)

    if prefix:
        tests_filter["testID"] = f"ilike.{prefix}"

    tests_rows = await sb_select("tests", tests_filter, order="testID")

    submissions: List[SubmissionSummary] = []
    if tests_rows:
        test_ids = [str(t["testID"]) for t in tests_rows]
        ids_csv = ",".join(test_ids)
        subs_rows = await sb_select(
            "student_tests",
            {
                "user_id": f"eq.{user_id}",
                "test_id": f"in.({ids_csv})",
                "submitted_at": "not.is.null",
            },
            select_cols="id,test_id,result_url,submitted_at",
        )
        submissions = [SubmissionSummary(**r) for r in subs_rows]

    return TestsByPrefixOut(tests=tests_rows, submissions=submissions)


# ── Session management ────────────────────────────────────────────────────────

async def start_or_resume(
    test_id: str, user_id: str, *, is_reattempt: bool = False
) -> Optional[StartTestOut]:
    """Find an existing unsubmitted session or create a new one."""
    if not is_reattempt:
        rows = await sb_select(
            "student_tests",
            {
                "test_id": f"eq.{test_id}",
                "user_id": f"eq.{user_id}",
                "submitted_at": "is.null",
            },
            select_cols="id,started_at,answers",
            order="started_at.desc",
            limit=1,
        )
        if rows:
            r = rows[0]
            return StartTestOut(
                id=r["id"],
                started_at=r.get("started_at", ""),
                answers=r.get("answers"),
            )

    from datetime import datetime, timezone
    row = await sb_insert(
        "student_tests",
        {
            "test_id": test_id,
            "user_id": user_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return StartTestOut(id=row["id"], started_at=row["started_at"])


async def save_answers(student_test_id: str, answers: dict) -> None:
    await sb_update(
        "student_tests",
        {"id": f"eq.{student_test_id}"},
        {"answers": answers},
        prefer_minimal=True,
    )


async def submit_test(student_test_id: str, answers: dict) -> SubmitTestOut:
    from datetime import datetime, timezone
    await sb_update(
        "student_tests",
        {"id": f"eq.{student_test_id}"},
        {"answers": answers, "submitted_at": datetime.now(timezone.utc).isoformat()},
        prefer_minimal=True,
    )
    return SubmitTestOut(submission_id=student_test_id)


async def get_attempts(test_id: str, user_id: str) -> List[AttemptOut]:
    rows = await sb_select(
        "student_tests",
        {
            "test_id": f"eq.{test_id}",
            "user_id": f"eq.{user_id}",
            "submitted_at": "not.is.null",
        },
        select_cols="id,submitted_at,started_at",
        order="submitted_at.desc",
    )
    return [AttemptOut(**r) for r in rows]


async def get_result(submission_id: str) -> Optional[TestResultOut]:
    rows = await sb_select(
        "student_tests",
        {"id": f"eq.{submission_id}"},
        select_cols="id,test_id,submitted_at,started_at,result_url,answers",
    )
    if not rows:
        return None
    record = rows[0]

    # Backfill result_url if the test was submitted but never scored
    if record.get("submitted_at") and not record.get("result_url"):
        try:
            from app.services.score_service import score_service
            github_url = await score_service.compute_and_persist_score(
                submission_id,
                record["test_id"],
                record.get("answers") or {},
            )
            record = {**record, "result_url": github_url}
        except Exception as exc:
            logger.warning("result_url backfill failed for %s: %s", submission_id, exc)

    # Strip answers — not part of TestResultOut schema
    record = {k: v for k, v in record.items() if k != "answers"}

    from app.services.test_resolver import try_resolve_test
    resolution = await try_resolve_test(record["test_id"])
    if resolution:
        record = {**record, "exam": resolution.meta.exam, "type": resolution.meta.type}

    return TestResultOut(**record)


async def get_meta(test_id: str) -> Optional[TestMetaOut]:
    rows = await sb_select(
        "tests",
        {"testID": f"eq.{test_id}"},
        select_cols='"99ile"',
    )
    if not rows:
        return None
    return TestMetaOut(percentile_99=rows[0].get("99ile"))


# ── LEGACY endpoints — remove once prepaired-web migrates to get_visible_tests ──

async def get_tests_and_submissions(user_id: str) -> TestsByPrefixOut:
    """LEGACY: used by GET /tests/submissions (prepaired-web Tests.tsx)."""
    import asyncio
    from app.schemas.test import TestsAndSubmissionsOut  # noqa: F401 — kept for symmetry

    tests_task = sb_select("tests", {}, order="testID")
    subs_task = sb_select(
        "student_tests",
        {
            "user_id": f"eq.{user_id}",
            "submitted_at": "not.is.null",
        },
        select_cols="id,test_id,result_url,submitted_at",
        order="submitted_at.desc",
    )
    tests_rows, subs_rows = await asyncio.gather(tests_task, subs_task)
    return TestsByPrefixOut(
        tests=tests_rows,
        submissions=[SubmissionSummary(**r) for r in subs_rows],
    )


async def get_tests_by_prefix(prefix: str, user_id: Optional[str]) -> TestsByPrefixOut:
    """LEGACY: used by GET /tests/by-prefix (prepaired-web Pyq2026.tsx)."""
    tests_rows = await sb_select("tests", {"testID": f"ilike.{prefix}"}, order="testID")

    submissions: List[SubmissionSummary] = []
    if user_id and tests_rows:
        ids_csv = ",".join(str(t["testID"]) for t in tests_rows)
        subs_rows = await sb_select(
            "student_tests",
            {
                "user_id": f"eq.{user_id}",
                "test_id": f"in.({ids_csv})",
                "submitted_at": "not.is.null",
            },
            select_cols="id,test_id,result_url,submitted_at",
        )
        submissions = [SubmissionSummary(**r) for r in subs_rows]

    return TestsByPrefixOut(tests=tests_rows, submissions=submissions)


async def get_tests_by_exam(exam: str, user_id: Optional[str]) -> TestsByPrefixOut:
    """LEGACY: used by GET /tests/by-exam (prepaired-web AIPTPage.tsx)."""
    tests_rows = await sb_select("tests", {"exam": f"eq.{exam}"}, order="testID")

    submissions: List[SubmissionSummary] = []
    if user_id and tests_rows:
        ids_csv = ",".join(str(t["testID"]) for t in tests_rows)
        subs_rows = await sb_select(
            "student_tests",
            {
                "user_id": f"eq.{user_id}",
                "test_id": f"in.({ids_csv})",
                "submitted_at": "not.is.null",
            },
            select_cols="id,test_id,result_url,submitted_at",
        )
        submissions = [SubmissionSummary(**r) for r in subs_rows]

    return TestsByPrefixOut(tests=tests_rows, submissions=submissions)


# ── End legacy ────────────────────────────────────────────────────────────────

async def get_student_tests_by_ids(ids: List[str]) -> List[StudentTestByIdOut]:
    """Batch-fetch student_test records by a list of IDs."""
    if not ids:
        return []
    ids_csv = ",".join(ids)
    rows = await sb_select(
        "student_tests",
        {"id": f"in.({ids_csv})"},
        select_cols="id,test_id",
    )
    return [StudentTestByIdOut(**r) for r in rows]
