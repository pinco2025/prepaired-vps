import logging
from typing import Any, Dict, Optional

from app.services.supabase_client import SupabaseError, sb_select

logger = logging.getLogger(__name__)

_TIER_ALIASES: Dict[str, str] = {
    # Minor spelling variants — all map to the canonical name
    "adv-26":       "adv26",
    "adv_26":       "adv26",
    "adv 26":       "adv26",
    "ipft-01-2026": "adv26",   # IPFT plan includes ADV content
    # Add future plan aliases here
}


def normalize_subscription_tier(raw: Optional[str]) -> Optional[str]:
    """Canonicalise tier/plan labels so equivalent values compare the same way."""
    if raw is None:
        return None
    value = " ".join(str(raw).strip().lower().split())
    if not value:
        return None
    return _TIER_ALIASES.get(value, value)


def user_can_access_tier(user_tier: Optional[str], required_tier: Optional[str]) -> bool:
    """Return True when the user's subscription grants full access to the content tier."""
    normalized_user_tier = normalize_subscription_tier(user_tier)
    normalized_required_tier = normalize_subscription_tier(required_tier)

    if normalized_required_tier == "free":
        return True
    if not normalized_user_tier:
        return False
    if not normalized_required_tier:
        return True
    return normalized_user_tier == normalized_required_tier


async def get_user_subscription_tier(
    user_id: Optional[str],
    jwt_payload: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Read the user's paid tier.

    Resolution order (fastest to slowest):
    1. JWT app_metadata / user_metadata — zero-latency, no extra HTTP call.
    2. Supabase DB: subscription_tier column (canonical).
    3. Supabase DB: subscription_type column (legacy fallback).

    Logs every step at DEBUG so production issues are immediately diagnosable.
    """
    if not user_id:
        return None

    # ── 1. JWT metadata (fastest — already decoded, no network) ────────────────
    if jwt_payload:
        for meta_key in ("app_metadata", "user_metadata"):
            meta: Dict[str, Any] = jwt_payload.get(meta_key) or {}
            for col in ("subscription_tier", "subscription_type"):
                raw = meta.get(col)
                if isinstance(raw, str) and raw.strip():
                    tier = normalize_subscription_tier(raw.strip())
                    logger.debug(
                        "get_user_subscription_tier: user_id=%s found tier=%s in jwt.%s.%s",
                        user_id, tier, meta_key, col,
                    )
                    return tier

    # ── 2 & 3. Supabase DB lookup (single query for both columns) ──────────────
    try:
        rows = await sb_select(
            "users",
            {"id": f"eq.{user_id}"},
            select_cols="subscription_tier,subscription_type",
            limit=1,
        )
        if rows:
            row = rows[0]
            for column in ("subscription_tier", "subscription_type"):
                raw = row.get(column)
                if isinstance(raw, str) and raw.strip():
                    tier = normalize_subscription_tier(raw.strip())
                    logger.debug(
                        "get_user_subscription_tier: user_id=%s found tier=%s in db.users.%s",
                        user_id, tier, column,
                    )
                    return tier
            logger.warning(
                "get_user_subscription_tier: user_id=%s — DB row found but both "
                "subscription_tier and subscription_type are empty/null. row=%r",
                user_id, row,
            )
        else:
            logger.warning(
                "get_user_subscription_tier: user_id=%s — no row in users table. "
                "User may not have a profile row.",
                user_id,
            )
    except SupabaseError as exc:
        # Combined query failed (e.g., one column doesn't exist).
        # Fall back to querying each column individually.
        logger.warning(
            "get_user_subscription_tier: combined column query failed for user_id=%s "
            "(status=%s). Falling back to per-column queries.",
            user_id, exc.status,
        )
        for column in ("subscription_tier", "subscription_type"):
            value = await _get_optional_user_column(user_id, column)
            if value:
                tier = normalize_subscription_tier(value)
                logger.debug(
                    "get_user_subscription_tier: user_id=%s found tier=%s in db.users.%s (fallback)",
                    user_id, tier, column,
                )
                return tier

    logger.warning(
        "get_user_subscription_tier: user_id=%s — no tier found in JWT or DB. "
        "User will be treated as free. Check Supabase users table and JWT claims.",
        user_id,
    )
    return None


async def _get_optional_user_column(user_id: str, column: str) -> Optional[str]:
    try:
        rows = await sb_select(
            "users",
            {"id": f"eq.{user_id}"},
            select_cols=column,
            limit=1,
        )
    except SupabaseError as exc:
        if _is_missing_column_error(exc, column):
            logger.info("Skipping optional users.%s lookup — column unavailable", column)
            return None
        # Log any other Supabase error so it's visible in prod logs
        logger.error(
            "Supabase error fetching users.%s for user_id=%s: status=%s detail=%s",
            column, user_id, exc.status, exc.detail,
        )
        raise

    if not rows:
        logger.warning(
            "_get_optional_user_column: no row found in users table for user_id=%s — "
            "user may not have a profile row.",
            user_id,
        )
        return None

    raw = rows[0].get(column)
    if not isinstance(raw, str):
        logger.debug(
            "_get_optional_user_column: users.%s for user_id=%s is %r (non-string) — skipping",
            column, user_id, raw,
        )
        return None

    return raw.strip() or None


def _is_missing_column_error(exc: SupabaseError, column: str) -> bool:
    detail = str(exc.detail).lower()
    return "column" in detail and column.lower() in detail
