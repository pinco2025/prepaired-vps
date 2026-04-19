"""
Supabase REST client helper.

This client interacts with tables (student_tests, student_sets, user_feedback) 
that reside permanently in Supabase. 

CRITICAL NOTE: Supabase and the Postgres Question DB will NOT be merged for a 
very long time. Supabase stays hosted online independently. This wrapper acts as 
the long-term access layer to those remote tables.
"""

from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

_BASE = settings.SUPABASE_URL.rstrip("/")
_HEADERS = {
    "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


class SupabaseError(Exception):
    def __init__(self, status: int, detail: Any):
        self.status = status
        self.detail = detail
        super().__init__(f"Supabase {status}: {detail}")


async def _request(
    method: str,
    table: str,
    *,
    params: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Any:
    headers = {**_HEADERS, **(extra_headers or {})}
    url = f"{_BASE}/rest/v1/{table}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.request(method, url, params=params, json=body, headers=headers)
    if not resp.is_success:
        raise SupabaseError(resp.status_code, resp.text)
    if not resp.content:
        return None
    return resp.json()


# ── CRUD helpers ───────────────────────────────────────────────────────────────

async def sb_select(
    table: str,
    filters: Dict[str, str],
    *,
    select_cols: str = "*",
    order: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    params: Dict[str, str] = {"select": select_cols}
    for col, val in filters.items():
        params[col] = val
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(limit)
    return await _request("GET", table, params=params)


async def sb_insert(table: str, data: Dict[str, Any]) -> Dict[str, Any]:
    rows = await _request("POST", table, body=data)
    return rows[0] if isinstance(rows, list) else rows


async def sb_upsert(
    table: str,
    data: Dict[str, Any],
    *,
    on_conflict: str,
) -> Dict[str, Any]:
    extra_headers = {"Prefer": "resolution=merge-duplicates,return=representation"}
    rows = await _request(
        "POST", table,
        params={"on_conflict": on_conflict},
        body=data,
        extra_headers=extra_headers,
    )
    return rows[0] if isinstance(rows, list) else rows


async def sb_update(
    table: str,
    filters: Dict[str, str],
    data: Dict[str, Any],
    *,
    prefer_minimal: bool = False,
) -> Optional[Dict[str, Any]]:
    headers = {"Prefer": "return=minimal"} if prefer_minimal else {}
    params = {k: v for k, v in filters.items()}
    rows = await _request("PATCH", table, params=params, body=data, extra_headers=headers)
    if prefer_minimal:
        return None
    return rows[0] if isinstance(rows, list) and rows else None


async def sb_delete(table: str, filters: Dict[str, str]) -> None:
    params = {k: v for k, v in filters.items()}
    await _request("DELETE", table, params=params)
