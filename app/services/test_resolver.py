"""
Unified test resolver — the single source of truth for routing between
dynamic_tests (generated) and tests (curated) tables.

Routing rule: IDs prefixed with "gen_" belong to dynamic_tests; all others
belong to tests. One Supabase round-trip per lookup. Manifests for generated
tests are cached in-process (immutable data, no TTL needed).
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, Literal, Optional

from app.services.supabase_client import sb_select

logger = logging.getLogger(__name__)

_GEN_PREFIX = "gen_"


# ── In-process LRU cache for generated test manifests ────────────────────────

class _LRUCache:
    def __init__(self, maxsize: int = 2048) -> None:
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._lock = Lock()
        self._maxsize = maxsize

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if key not in self._cache:
                return None
            self._cache.move_to_end(key)
            return self._cache[key]

    def put(self, key: str, value: Dict[str, Any]) -> None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)


_dynamic_cache = _LRUCache(maxsize=2048)


# ── Public types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TestMeta:
    id: str
    exam: str
    type: Literal["curated", "generated"]
    title: str
    duration: int
    total_marks: int


@dataclass(frozen=True)
class TestResolution:
    meta: TestMeta
    raw: Dict[str, Any]


class TestNotFound(Exception):
    pass


# ── Resolver ──────────────────────────────────────────────────────────────────

async def resolve_test(test_id: str) -> TestResolution:
    """
    Route test_id to the correct Supabase table and return a TestResolution.

    - gen_* → dynamic_tests (with in-process LRU cache)
    - anything else → tests

    Raises TestNotFound if the row doesn't exist.
    Exactly one Supabase round-trip per cache miss.
    """
    if test_id.startswith(_GEN_PREFIX):
        cached = _dynamic_cache.get(test_id)
        if cached is not None:
            logger.debug("resolve_test: cache hit for %s", test_id)
            return _resolution_from_dynamic(cached)

        rows = await sb_select("dynamic_tests", {"id": f"eq.{test_id}"}, limit=1)
        if not rows:
            raise TestNotFound(f"Generated test '{test_id}' not found in dynamic_tests")
        _dynamic_cache.put(test_id, rows[0])
        return _resolution_from_dynamic(rows[0])

    rows = await sb_select("tests", {"testID": f"eq.{test_id}"}, limit=1)
    if not rows:
        raise TestNotFound(f"Curated test '{test_id}' not found in tests")
    return _resolution_from_curated(rows[0])


async def try_resolve_test(test_id: str) -> Optional[TestResolution]:
    """Same as resolve_test but returns None instead of raising on missing tests."""
    try:
        return await resolve_test(test_id)
    except TestNotFound:
        return None


# ── Internal builders ─────────────────────────────────────────────────────────

def _resolution_from_dynamic(row: Dict[str, Any]) -> TestResolution:
    meta = TestMeta(
        id=row["id"],
        exam=row["exam"],
        type="generated",
        title=row["title"],
        duration=row["duration"],
        total_marks=row["total_marks"],
    )
    return TestResolution(meta=meta, raw=row)


def _resolution_from_curated(row: Dict[str, Any]) -> TestResolution:
    meta = TestMeta(
        id=row.get("testID", ""),
        exam=row.get("exam", ""),
        type="curated",
        title=row.get("title", ""),
        duration=row.get("duration", 0) or 0,
        total_marks=row.get("total_marks", 0) or 0,
    )
    return TestResolution(meta=meta, raw=row)
