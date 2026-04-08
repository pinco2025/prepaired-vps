"""
Practice set session service — wraps Supabase REST (student_sets table).
closeSessionOnUnload now hits this backend instead of leaking credentials.
"""

from datetime import datetime, timezone
from typing import Optional

from app.schemas.set import SetResumeOut, StartSetOut
from app.services.supabase_client import sb_insert, sb_select, sb_update


async def get_latest_session(set_id: str, user_id: str) -> Optional[SetResumeOut]:
    rows = await sb_select(
        "student_sets",
        {"user_id": f"eq.{user_id}", "set_id": f"eq.{set_id}"},
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
    """Mark session closed (time_elapsed = -1). Called from keepalive endpoint."""
    await sb_update(
        "student_sets",
        {"id": f"eq.{session_id}"},
        {"time_elapsed": -1},
        prefer_minimal=True,
    )
