"""
Test session service — thin wrapper over Supabase REST (student_tests table).
All business logic lives here so the router stays clean.
"""

from typing import List, Optional

from app.schemas.test import (
    AttemptOut,
    StartTestOut,
    StudentTestByIdOut,
    SubmitTestOut,
    SubmissionSummary,
    TestMetaOut,
    TestResultOut,
    TestsAndSubmissionsOut,
    TestsByPrefixOut,
)
from app.services.supabase_client import sb_insert, sb_select, sb_update


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


async def submit_test(
    student_test_id: str, answers: dict
) -> SubmitTestOut:
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
    import asyncio
    rows = await sb_select(
        "student_tests",
        {"id": f"eq.{submission_id}"},
        select_cols="id,test_id,submitted_at,started_at,result_url",
    )
    if not rows:
        return None
    record = rows[0]

    # Fetch exam/type from tests table in parallel to avoid extra round-trip latency
    test_rows = await sb_select(
        "tests",
        {"testID": f"eq.{record['test_id']}"},
        select_cols="exam,type",
        limit=1,
    )
    if test_rows:
        record = {**record, "exam": test_rows[0].get("exam"), "type": test_rows[0].get("type")}

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


async def get_tests_and_submissions(user_id: str) -> TestsAndSubmissionsOut:
    """Parallel fetch: all tests (ordered by testID) + user's submitted student_tests."""
    import asyncio

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

    return TestsAndSubmissionsOut(
        tests=tests_rows,
        submissions=[SubmissionSummary(**r) for r in subs_rows],
    )


async def get_tests_by_prefix(prefix: str, user_id: Optional[str]) -> TestsByPrefixOut:
    """Tests whose testID matches the ilike pattern, plus user's submissions for those tests."""
    tests_rows = await sb_select(
        "tests",
        {"testID": f"ilike.{prefix}"},
        order="testID",
    )

    submissions: List[SubmissionSummary] = []
    if user_id and tests_rows:
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
