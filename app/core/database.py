from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# ── Engine ─────────────────────────────────────────────────────────────────────
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # Detect stale connections
    echo=settings.ENVIRONMENT == "development",
)

# ── Session factory ────────────────────────────────────────────────────────────
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# ── Base model ─────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Dependency ─────────────────────────────────────────────────────────────────
async def get_db() -> AsyncSession:
    """FastAPI dependency that yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
