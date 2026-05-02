"""
Audience tag resolution for personalised test targeting.

Call `get_user_audience_tags(user_id)` to obtain the set of tags that describe
a user's onboarding profile, then pass those tags to `_build_visibility_filter`
in test_service.py to scope PostgREST queries.

Onboarding fields (stored in Supabase `users` table):
  exam_type        — 'JEE' | 'NEET'
  user_level       — '11' | '12' | 'dropper'
  onboarding_prefs — JSONB blob from OnboardingScreen.tsx
"""

from typing import Any, Dict, List, Optional, Tuple
import logging

from app.services.supabase_client import sb_select, SupabaseError

logger = logging.getLogger(__name__)


def derive_audience_tags(profile: Dict[str, Any]) -> List[str]:
    """
    Pure function: maps a user profile dict to a list of audience tag strings.
    Mirrors the onboarding fields saved in OnboardingScreen.tsx:671-685.
    """
    tags: List[str] = []

    exam = (profile.get("exam_type") or "").strip().upper()
    if exam == "JEE":
        tags.append("exam:jee")
    elif exam == "NEET":
        tags.append("exam:neet")

    level = (profile.get("user_level") or "").strip().lower()
    if level in ("11", "12", "dropper"):
        tags.append(f"level:{level}")

    prefs: Optional[Dict[str, Any]] = profile.get("onboarding_prefs")
    if isinstance(prefs, dict):
        # Syllabus focus (JEE class 12)
        syl12 = prefs.get("syl12") or {}
        if syl12.get("enabled"):
            tags.append("track:syl12")
            focus = (syl12.get("focus") or "").strip().lower()
            if focus == "mains":
                tags.append("focus:mains")
            elif focus == "advanced":
                tags.append("focus:advanced")

        # Class 11 revision track
        rev11 = prefs.get("rev11") or {}
        if rev11.get("enabled"):
            depth = (rev11.get("depth") or "").strip().lower()
            tags.append("track:rev11:full" if depth == "full" else "track:rev11:min")

        # JEE Advanced revision (class 12 only)
        rev11_adv = prefs.get("rev11Adv") or {}
        if rev11_adv.get("enabled"):
            tags.append("track:rev11adv")

        # Dropper class 12 revision
        rev12 = prefs.get("rev12") or {}
        if rev12.get("enabled"):
            depth = (rev12.get("depth") or "").strip().lower()
            tags.append("track:rev12:full" if depth == "full" else "track:rev12:min")

    return tags


async def get_user_audience_tags(user_id: str) -> List[str]:
    """Fetches the user's profile from Supabase and derives their audience tags."""
    _, tags = await get_user_profile(user_id)
    return tags


async def get_user_profile(user_id: str) -> Tuple[Optional[str], List[str]]:
    """
    Returns (exam_type, audience_tags) for the user in a single Supabase query.
    exam_type is 'JEE' | 'NEET' | None.
    """
    try:
        rows = await sb_select(
            "users",
            {"id": f"eq.{user_id}"},
            select_cols="exam_type,user_level,onboarding_prefs",
            limit=1,
        )
    except SupabaseError as exc:
        logger.warning(
            "get_user_profile: Supabase error for user_id=%s (status=%s). "
            "Falling back to empty profile.",
            user_id, exc.status,
        )
        return None, []

    if not rows:
        logger.warning(
            "get_user_profile: no users row for user_id=%s — treating as untagged",
            user_id,
        )
        return None, []

    profile = rows[0]
    exam_type = (profile.get("exam_type") or "").strip().upper() or None
    tags = derive_audience_tags(profile)
    return exam_type, tags
