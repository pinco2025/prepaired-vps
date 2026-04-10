"""
JWT verification using PyJWT + the Supabase JWT secret.
No HTTP roundtrip to /auth/v1/user — purely local crypto.
"""

import base64
from typing import Optional, Union

import jwt
from jwt.exceptions import PyJWTError

from app.core.config import settings


class TokenPayload:
    def __init__(self, sub: str, email: Optional[str], role: Optional[str]):
        self.sub = sub          # user UUID (Supabase user ID)
        self.email = email
        self.role = role        # "authenticated", "anon", etc.


def _decode_jwt_secret() -> Union[str, bytes]:
    """
    Supabase stores the JWT secret as a base64-encoded value in the dashboard.
    Decode it to raw bytes so PyJWT uses the same key Supabase signed with.
    Falls back to the raw string if it isn't valid base64.
    """
    raw = settings.SUPABASE_JWT_SECRET
    try:
        return base64.b64decode(raw)
    except Exception:
        return raw


_JWT_SECRET = _decode_jwt_secret()


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
    )
