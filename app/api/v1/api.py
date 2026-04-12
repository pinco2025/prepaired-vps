from fastapi import APIRouter

from app.api.v1 import (
    diagnostic,
    feedback,
    misc,
    question_sets,
    questions,
    scores,
    sets,
    tests,
)

api_router = APIRouter()

api_router.include_router(questions.router)
api_router.include_router(tests.router)
api_router.include_router(sets.router)
api_router.include_router(scores.router)
api_router.include_router(feedback.router)
api_router.include_router(diagnostic.router)
api_router.include_router(question_sets.router)
api_router.include_router(misc.router)
