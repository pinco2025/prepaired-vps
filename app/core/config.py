from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # ── Database ──────────────────────────────────────────────────────────────
    # Dedicated PostgreSQL DB explicitly for questions, solutions, and content.
    # CRITICAL: This is permanently isolated from Supabase and will NOT be merged.
    DATABASE_URL: str

    # ── Supabase ──────────────────────────────────────────────────────────────
    # Hosted online separately for Auth, Sessions, Tests, and User Feedback.
    SUPABASE_URL: str
    SUPABASE_JWT_SECRET: str
    SUPABASE_SERVICE_ROLE_KEY: str

    # ── CORS ──────────────────────────────────────────────────────────────────
    ALLOWED_ORIGINS: str = "https://www.prepaired.site,http://localhost:3000"

    # ── App ───────────────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"
    FREE_QUESTION_LIMIT: int = 10

    # ── GitHub (Legacy Score Push) ────────────────────────────────────────────
    GITHUB_TOKEN: str | None = None
    GITHUB_REPO: str | None = None

    @property
    def allowed_origins_list(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",")]


settings = Settings()
