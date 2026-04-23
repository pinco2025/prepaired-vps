"""
Analytics endpoints — read/write aggregates from pyq_attempts + study_sessions.
All user data lives in Supabase (service-role key used server-side).

Write:
  POST /analytics/sessions      — record a PYQ practice session
  POST /analytics/pyq-attempts  — bulk-record per-question PYQ verdicts

Read:
  GET  /analytics/attempts-breakdown
  GET  /analytics/study-time?days=7
  GET  /analytics/weak-chapters?limit=5
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.deps import get_current_user
from app.core.security import TokenPayload
from app.services.supabase_client import sb_bulk_insert, sb_insert, sb_select

router = APIRouter(prefix="/analytics", tags=["analytics"])


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class SessionIn(BaseModel):
    source: str          # 'pyq' | 'set'
    duration_sec: int
    started_at: Optional[str] = None


class PYQAttemptItem(BaseModel):
    question_id: str
    subject: str
    chapter_code: str
    chapter_name: str
    exam: str
    verdict: str         # 'correct' | 'incorrect' | 'skipped'


class PYQAttemptsIn(BaseModel):
    attempts: List[PYQAttemptItem]


# ── Write endpoints ────────────────────────────────────────────────────────────

@router.post("/sessions", status_code=201)
async def record_session(
    body: SessionIn,
    user: TokenPayload = Depends(get_current_user),
):
    """Record a PYQ practice session duration. Called by frontend on screen unmount."""
    now = datetime.now(timezone.utc).isoformat()
    await sb_insert("study_sessions", {
        "user_id": user.sub,
        "source": body.source,
        "duration_sec": max(0, body.duration_sec),
        "correct_count": 0,
        "incorrect_count": 0,
        "skipped_count": 0,
        "started_at": body.started_at or now,
        "ended_at": now,
    })
    return {"ok": True}


@router.post("/pyq-attempts", status_code=201)
async def record_pyq_attempts(
    body: PYQAttemptsIn,
    user: TokenPayload = Depends(get_current_user),
):
    """Bulk-record per-question PYQ verdicts. Called by frontend on screen unmount."""
    valid_verdicts = {"correct", "incorrect", "skipped"}
    now = datetime.now(timezone.utc).isoformat()

    rows = [
        {
            "user_id": user.sub,
            "question_id": a.question_id,
            "subject": a.subject,
            "chapter_code": a.chapter_code,
            "chapter_name": a.chapter_name,
            "exam": a.exam,
            "verdict": a.verdict,
            "attempted_at": now,
        }
        for a in body.attempts
        if a.verdict in valid_verdicts
    ]

    if rows:
        await sb_bulk_insert("pyq_attempts", rows)

    return {"ok": True, "inserted": len(rows)}


# ── Read endpoints ─────────────────────────────────────────────────────────────

@router.get("/attempts-breakdown")
async def get_attempts_breakdown(user: TokenPayload = Depends(get_current_user)):
    """
    Aggregated correct/incorrect/skipped from:
      - pyq_attempts  (PYQ practice, per-question verdicts)
      - study_sessions where source='set'  (stored on set submit)
    """
    pyq_rows = await sb_select(
        "pyq_attempts",
        {"user_id": f"eq.{user.sub}"},
        select_cols="verdict",
    )
    pyq_correct = sum(1 for r in pyq_rows if r.get("verdict") == "correct")
    pyq_incorrect = sum(1 for r in pyq_rows if r.get("verdict") == "incorrect")
    pyq_skipped = sum(1 for r in pyq_rows if r.get("verdict") == "skipped")

    set_rows = await sb_select(
        "study_sessions",
        {"user_id": f"eq.{user.sub}", "source": "eq.set"},
        select_cols="correct_count,incorrect_count,skipped_count",
    )
    set_correct = sum(r.get("correct_count") or 0 for r in set_rows)
    set_incorrect = sum(r.get("incorrect_count") or 0 for r in set_rows)
    set_skipped = sum(r.get("skipped_count") or 0 for r in set_rows)

    total_correct = pyq_correct + set_correct
    total_incorrect = pyq_incorrect + set_incorrect
    total_skipped = pyq_skipped + set_skipped

    return {
        "total": total_correct + total_incorrect + total_skipped,
        "correct": total_correct,
        "incorrect": total_incorrect,
        "skipped": total_skipped,
        "sources": {
            "pyq": len(pyq_rows) > 0,
            "sets": len(set_rows) > 0,
        },
    }


@router.get("/study-time")
async def get_study_time(
    days: int = Query(default=7, ge=1, le=90),
    user: TokenPayload = Depends(get_current_user),
):
    """Daily study hours over the last N days (oldest → newest)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    rows = await sb_select(
        "study_sessions",
        {"user_id": f"eq.{user.sub}", "ended_at": f"gte.{cutoff}"},
        select_cols="duration_sec,ended_at",
    )

    today = datetime.now(timezone.utc).date()
    day_map: dict[str, float] = {}
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        day_map[d] = 0.0

    for row in rows:
        ended_at = row.get("ended_at")
        if not ended_at:
            continue
        try:
            dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
            date_str = dt.date().isoformat()
            if date_str in day_map:
                day_map[date_str] += (row.get("duration_sec") or 0) / 3600
        except (ValueError, TypeError):
            pass

    points = [
        {"date": d, "hours": round(h, 2)}
        for d, h in sorted(day_map.items())
    ]
    return {
        "total_hours": round(sum(p["hours"] for p in points), 2),
        "points": points,
    }


@router.get("/weak-chapters")
async def get_weak_chapters(
    limit: int = Query(default=5, ge=1, le=20),
    user: TokenPayload = Depends(get_current_user),
):
    """
    Top-N weakest chapters by accuracy, computed from pyq_attempts.
    Chapters with fewer than 3 recorded attempts are excluded (not enough signal).
    """
    rows = await sb_select(
        "pyq_attempts",
        {"user_id": f"eq.{user.sub}"},
        select_cols="chapter_code,chapter_name,subject,verdict",
    )

    # Aggregate per chapter
    chapter_data: dict[str, dict] = {}
    for row in rows:
        code = row.get("chapter_code", "")
        if not code:
            continue
        if code not in chapter_data:
            chapter_data[code] = {
                "chapter_name": row.get("chapter_name") or code,
                "subject": row.get("subject", ""),
                "attempted": 0,
                "correct": 0,
            }
        chapter_data[code]["attempted"] += 1
        if row.get("verdict") == "correct":
            chapter_data[code]["correct"] += 1

    items = []
    for code, data in chapter_data.items():
        attempted = data["attempted"]
        if attempted < 3:
            continue
        accuracy = data["correct"] / attempted
        # weakness: low accuracy weighted by volume (caps at 10 attempts for full weight)
        weakness_score = (1 - accuracy) * min(attempted / 10, 1.0)
        items.append({
            "subject": data["subject"],
            "chapterCode": code,
            "chapterName": data["chapter_name"],
            "accuracy": round(accuracy, 3),
            "attempted": attempted,
            "weaknessScore": round(weakness_score, 3),
        })

    items.sort(key=lambda x: x["weaknessScore"], reverse=True)
    return {"items": items[:limit]}
