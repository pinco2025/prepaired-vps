import logging
from typing import Optional

from app.services.supabase_client import SupabaseError, sb_select

logger = logging.getLogger(__name__)

_TIER_ALIASES = {
    "adv-26": "adv26",
    "adv_26": "adv26",
    "adv 26": "adv26",
}


def normalize_subscription_tier(raw: Optional[str]) -> Optional[str]:
    """Canonicalise tier labels so equivalent ADV-26 variants compare the same way."""
    if raw is None:
        return None
    value = " ".join(str(raw).strip().lower().split())
    if not value:
        return None
    return _TIER_ALIASES.get(value, value)


def user_can_access_tier(user_tier: Optional[str], required_tier: Optional[str]) -> bool:
    """Return true when the user's subscription grants full access to the content tier."""
    normalized_user_tier = normalize_subscription_tier(user_tier)
    normalized_required_tier = normalize_subscription_tier(required_tier)

    if normalized_required_tier == "free":
        return True
    if not normalized_user_tier:
        return False
    if not normalized_required_tier:
        return True
    return normalized_user_tier == normalized_required_tier


async def get_user_subscription_tier(user_id: Optional[str]) -> Optional[str]:
    """
    Read the user's paid tier from Supabase.

    `subscription_tier` is the current canonical column. `subscription_type` is
    queried as a compatibility fallback for environments that still use the old
    field name.
    """
    if not user_id:
        return None

    for column in ("subscription_tier", "subscription_type"):
        value = await _get_optional_user_column(user_id, column)
        if value:
            return normalize_subscription_tier(value)

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
            logger.info("Skipping optional users.%s lookup because the column is unavailable", column)
            return None
        raise

    if not rows:
        return None

    raw = rows[0].get(column)
    if not isinstance(raw, str):
        return None

    return raw.strip() or None


def _is_missing_column_error(exc: SupabaseError, column: str) -> bool:
    detail = str(exc.detail).lower()
    return "column" in detail and column.lower() in detail
