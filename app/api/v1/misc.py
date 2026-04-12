"""
Remaining CRUD endpoints — predictor, analytics, question requests.

POST   /api/v1/predictor
GET    /api/v1/predictor
DELETE /api/v1/predictor

GET    /api/v1/analytics/user

POST   /api/v1/question-requests
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import get_current_user
from app.core.security import TokenPayload
from app.schemas.misc import (
    PredictorIn,
    PredictorOut,
    QuestionRequestIn,
    UserAnalyticsOut,
)
from app.services.supabase_client import sb_delete, sb_insert, sb_select

router = APIRouter(tags=["misc"])


# ── Predictor ──────────────────────────────────────────────────────────────────

@router.post("/predictor", response_model=PredictorOut, status_code=status.HTTP_201_CREATED)
async def upsert_predictor(
    body: PredictorIn,
    user: TokenPayload = Depends(get_current_user),
):
    # Upsert: delete existing, then insert fresh
    await sb_delete("jee_predictor", {"user_id": f"eq.{user.sub}"})
    row = await sb_insert("jee_predictor", {"user_id": user.sub, "data": body.data})
    return PredictorOut(id=row["id"], user_id=row["user_id"], data=row["data"], created_at=row.get("created_at"))


@router.get("/predictor", response_model=PredictorOut)
async def get_predictor(user: TokenPayload = Depends(get_current_user)):
    rows = await sb_select(
        "jee_predictor", {"user_id": f"eq.{user.sub}"}, select_cols="id,user_id,data,created_at", limit=1
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No predictor data found")
    r = rows[0]
    return PredictorOut(id=r["id"], user_id=r["user_id"], data=r["data"], created_at=r.get("created_at"))


@router.delete("/predictor", status_code=status.HTTP_204_NO_CONTENT)
async def delete_predictor(user: TokenPayload = Depends(get_current_user)):
    await sb_delete("jee_predictor", {"user_id": f"eq.{user.sub}"})


# ── Analytics ──────────────────────────────────────────────────────────────────

@router.get("/analytics/user", response_model=UserAnalyticsOut)
async def user_analytics(user: TokenPayload = Depends(get_current_user)):
    """
    Aggregates student_tests data for the AI Insights page.
    Returns raw data; heavy aggregation will move to a dedicated service later.
    Each submission includes history_url (aliased from result_url) so the
    Dashboard can navigate to the result without knowing the internal field name.
    """
    rows = await sb_select(
        "student_tests",
        {"user_id": f"eq.{user.sub}", "submitted_at": "not.is.null"},
        select_cols="id,test_id,answers,submitted_at,result_url",
        order="submitted_at.desc",
    )
    # Normalise: expose result_url under both its original name and history_url
    # so the frontend analyticsService can consume it without field-name gymnastics.
    for row in rows:
        row["history_url"] = row.get("result_url")
    return UserAnalyticsOut(data={"submissions": rows})


# ── Question requests ──────────────────────────────────────────────────────────

@router.post("/question-requests", status_code=status.HTTP_201_CREATED)
async def create_question_request(
    body: QuestionRequestIn,
    user: TokenPayload = Depends(get_current_user),
):
    await sb_insert("question_requests", {
        "user_id": user.sub,
        "subject": body.subject,
        "chapter": body.chapter,
        "details": body.details,
    })
    return {"status": "ok"}
