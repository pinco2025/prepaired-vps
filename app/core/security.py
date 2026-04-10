"""
JWT verification using PyJWT + the Supabase JWT secret.
No HTTP roundtrip to /auth/v1/user — purely local crypto.
"""

from typing import Optional, Union

import jwt
from jwt.exceptions import PyJWTError

from app.core.config import settings


class TokenPayload:
    def __init__(
        self,
        sub: str,
        email: Optional[str],
        role: Optional[str],
        raw_payload: Optional[dict] = None,
    ):
        self.sub = sub          # user UUID (Supabase user ID)
        self.email = email
        self.role = role        # "authenticated", "anon", etc.
        self.raw_payload = raw_payload or {}  # full decoded JWT claims


# Supabase signs JWTs with the raw UTF-8 bytes of the secret string.
# Do NOT base64-decode it — the dashboard shows the key as-is.
_JWT_SECRET: str = settings.SUPABASE_JWT_SECRET


def verify_token(token: str) -> TokenPayload:
    """
    Decode and verify a Supabase-issued JWT.
    Raises jwt.PyJWTError on any failure (expired, bad sig, malformed).
    """
    payload = jwt.decode(
        token,
        _JWT_SECRET,
        algorithms=["HS256"],
        options={"verify_aud": False},  # Supabase JWTs have audience = "authenticated"
    )
    return TokenPayload(
        sub=payload["sub"],
        email=payload.get("email"),
        role=payload.get("role"),
        raw_payload=payload,
    )
