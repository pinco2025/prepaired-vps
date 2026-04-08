"""
FastAPI dependency functions for auth + DB injection.
"""

from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from jwt.exceptions import PyJWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import TokenPayload, verify_token


# ── Auth dependencies ──────────────────────────────────────────────────────────

def _extract_token(authorization: Optional[str] = Header(default=None)) -> Optional[str]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization.removeprefix("Bearer ").strip()


async def get_current_user(
    token: Optional[str] = Depends(_extract_token),
) -> TokenPayload:
    """Require a valid Supabase JWT. Raises 401 if missing or invalid."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return verify_token(token)
    except PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_optional_user(
    token: Optional[str] = Depends(_extract_token),
) -> Optional[TokenPayload]:
    """
    Return the user payload if a valid JWT is present, else None.
    Used for endpoints that serve free-tier content without auth.
    """
    if not token:
        return None
    try:
        return verify_token(token)
    except PyJWTError:
        return None
