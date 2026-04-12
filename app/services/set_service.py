"""
Practice set session service — wraps Supabase REST (student_sets table).
closeSessionOnUnload now hits this backend instead of leaking credentials.
"""

from datetime import datetime, timezone
from typing import Any, Optional, Set

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.question import Question
from app.schemas.set import SetResumeOut, SetSubmitOut, StartSetOut
from app.services.supabase_client import sb_insert, sb_select, sb_update


async def get_latest_session(set_id: str, user_id: str) -> Optional[SetResumeOut]:
    """Return the newest UNSUBMITTED session for this user+set, or None."""
    rows = await sb_select(
        "student_sets",
        {
            "user_id": f"eq.{user_id}",
            "set_id": f"eq.{set_id}",
            "submitted_at": "is.null",
        },
        select_cols="id,answers",
        order="created_at.desc",
        limit=1,
    )
    if not rows:
        return None
    r = rows[0]
    return SetResumeOut(id=r["id"], answers=r.get("answers") or {})


async def create_session(
    set_id: str,
    user_id: str,
    questions_url: Optional[str] = None,
    initial_answers: Optional[dict] = None,
) -> StartSetOut:
    row = await sb_insert(
        "student_sets",
        {
            "user_id": user_id,
            "set_id": set_id,
            "answers": initial_answers or {},
            "questions_url": questions_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return StartSetOut(id=row["id"])


async def update_answers(session_id: str, answers: dict) -> None:
    await sb_update(
        "student_sets",
        {"id": f"eq.{session_id}"},
        {"answers": answers},
        prefer_minimal=True,
    )


async def update_time(session_id: str, time_elapsed: int) -> None:
    await sb_update(
        "student_sets",
        {"id": f"eq.{session_id}"},
        {"time_elapsed": time_elapsed},
        prefer_minimal=True,
    )


async def close_session(session_id: str) -> None:
    """Mark session closed (time_elapsed = -1). Kept for admin tooling; frontend no longer calls this."""
    await sb_update(
        "student_sets",
        {"id": f"eq.{session_id}"},
        {"time_elapsed": -1},
        prefer_minimal=True,
    )


def _matches(user_ans: Any, correct_ans: str, q_type: Optional[str]) -> bool:
    """
    Returns True if user_ans matches correct_ans.
    Handles MCQ (exact string), Integer/Numerical (exact string), and
    MultiCorrect (set equality, comma-separated or list).
    """
    if user_ans is None or user_ans == "":
        return False

    is_multi = q_type and q_type.lower() in ("multicorrect", "multi_correct", "multi correct")

    if is_multi:
        correct_set: Set[str] = {c.strip().upper() for c in correct_ans.split(",")} if correct_ans else set()
        if isinstance(user_ans, list):
            user_set: Set[str] = {str(a).strip().upper() for a in user_ans if a}
        else:
            user_set = {a.strip().upper() for a in str(user_ans).split(",") if a.strip()}
        return user_set == correct_set

    # MCQ / Integer / Numerical — exact string comparison
    return str(user_ans).strip() == str(correct_ans).strip()


async def submit_session(
    db: AsyncSession,
    session_id: str,
    user_id: str,
    final_answers: dict,
) -> SetSubmitOut:
    """
    Marks a session as submitted, scores the answers against Postgres question keys,
    and returns a simple breakdown (correct / incorrect / unattempted / accuracy).
    No marks, no section breakdown — sets are pure practice.
    """
    # 1. Load session, verify ownership, guard against double-submit
    rows = await sb_select(
        "student_sets",
        {"id": f"eq.{session_id}", "user_id": f"eq.{user_id}"},
        select_cols="id,answers,submitted_at",
        limit=1,
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
    if rows[0].get("submitted_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Session already submitted")

    # Merge stored answers with any final answers sent in the request body
    # (covers the race where the last save didn't complete before submit)
    merged: dict = {**(rows[0].get("answers") or {}), **final_answers}

    # 2. Look up correct answers for every UUID in the merged dict
    uuids = list(merged.keys())
    qmap: dict[str, tuple[str, Optional[str]]] = {}   # uuid → (correct_answer, q_type)
    if uuids:
        result = await db.execute(
            select(Question.id, Question.legacy_id, Question.answer, Question.type)
            .where(
                or_(
                    Question.id.in_(uuids),
                    Question.legacy_id.in_(uuids),
                )
            )
        )
        for row in result:
            # The answers dict is keyed by legacy_id (which is what the frontend sends)
            key = row.legacy_id or str(row.id)
            qmap[key] = (row.answer, row.type)

    # 3. Score
    correct = incorrect = 0
    for uuid, user_ans in merged.items():
        if uuid not in qmap:
            continue   # question removed from DB since session started — skip silently
        correct_ans, q_type = qmap[uuid]
        if user_ans is None or user_ans == "":
            continue   # unattempted — don't count as incorrect
        if _matches(user_ans, correct_ans, q_type):
            correct += 1
        else:
            incorrect += 1

    # total = number of questions the user actually saw (keys in qmap we could resolve)
    total = len(qmap) if qmap else len(merged)
    attempted = correct + incorrect
    unattempted = total - attempted
    accuracy = round((correct / attempted) * 100, 1) if attempted else 0.0

    # 4. Persist the submission timestamp
    submitted_at = datetime.now(timezone.utc).isoformat()
    await sb_update(
        "student_sets",
        {"id": f"eq.{session_id}"},
        {"answers": merged, "submitted_at": submitted_at},
        prefer_minimal=True,
    )

    return SetSubmitOut(
        session_id=session_id,
        total=total,
        correct=correct,
        incorrect=incorrect,
        unattempted=unattempted,
        accuracy=accuracy,
        submitted_at=submitted_at,
    )
