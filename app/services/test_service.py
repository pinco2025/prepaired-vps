"""
Test session service — thin wrapper over Supabase REST (student_tests table).
All business logic lives here so the router stays clean.
"""

from typing import List, Optional

from app.schemas.test import (
    AttemptOut,
    StartTestOut,
    SubmitTestOut,
    TestMetaOut,
    TestResultOut,
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
    rows = await sb_select(
        "student_tests",
        {"id": f"eq.{submission_id}"},
        select_cols="id,test_id,submitted_at,started_at,result_url",
    )
    if not rows:
        return None
    return TestResultOut(**rows[0])


async def get_meta(test_id: str) -> Optional[TestMetaOut]:
    rows = await sb_select(
        "tests",
        {"testID": f"eq.{test_id}"},
        select_cols='"99ile"',
    )
    if not rows:
        return None
    return TestMetaOut(percentile_99=rows[0].get("99ile"))
