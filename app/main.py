from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.api import api_router
from app.core.config import settings

app = FastAPI(
    title="prepAIred Backend API",
    description="FastAPI backend for prepAIred with Postgres and Supabase Auth",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# ── CORS Middleware ────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(api_router, prefix="/api/v1")


@app.get("/api/health")
async def health_check():
    """Basic health check endpoint."""
    return {"status": "ok", "environment": settings.ENVIRONMENT}
