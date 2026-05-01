import logging
from typing import Any, Dict, Optional

from app.services.supabase_client import SupabaseError, sb_select

logger = logging.getLogger(__name__)

_TIER_ALIASES: Dict[str, str] = {
    "adv-26":       "adv26",
    "adv_26":       "adv26",
    "adv 26":       "adv26",
    "ipft-01-2026": "adv26",
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
    """Read the user's paid tier from users.subscription_tier."""
    if not user_id:
        return None

    try:
        rows = await sb_select(
            "users",
            {"id": f"eq.{user_id}"},
            select_cols="subscription_tier",
            limit=1,
        )
    except SupabaseError as exc:
        logger.error(
            "get_user_subscription_tier: DB error for user_id=%s (status=%s): %s",
            user_id, exc.status, exc.detail,
        )
        return None

    if not rows:
        logger.warning(
            "get_user_subscription_tier: no row in users table for user_id=%s", user_id,
        )
        return None

    raw = rows[0].get("subscription_tier")
    if not isinstance(raw, str) or not raw.strip():
        logger.warning(
            "get_user_subscription_tier: user_id=%s — subscription_tier is empty/null", user_id,
        )
        return None

    tier = normalize_subscription_tier(raw.strip())
    logger.debug("get_user_subscription_tier: user_id=%s tier=%s", user_id, tier)
    return tier
