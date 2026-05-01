#!/usr/bin/env python3
"""
One-shot script: for every UUID in CSV/cluster_representatives.json,
find the question by legacy_id and append "MIN" to its used_in array.

Run from backend/ directory:
    python scripts/patch_used_in_min.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

JSON_PATH = Path(__file__).parent.parent / "CSV" / "cluster_representatives.json"

PATCH_SQL = """
    UPDATE questions
    SET used_in = array(
        SELECT DISTINCT unnest(array_append(used_in, 'MIN'))
    )
    WHERE legacy_id = $1
      AND NOT ('MIN' = ANY(used_in))
"""


async def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("ERROR: DATABASE_URL environment variable not set.")

    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    all_ids: list[str] = [uid for uids in data.values() for uid in uids]
    unique_ids = list(dict.fromkeys(all_ids))  # preserve order, deduplicate
    print(f"Loaded {len(unique_ids)} unique legacy_ids from {JSON_PATH.name}\n")

    print("Connecting to database…")
    conn = await asyncpg.connect(db_url)
    print("Connected.\n")

    updated = 0
    not_found = 0

    for legacy_id in unique_ids:
        status = await conn.execute(PATCH_SQL, legacy_id)
        if status == "UPDATE 1":
            updated += 1
        elif status == "UPDATE 0":
            # Either already has MIN or legacy_id not found — check which
            exists = await conn.fetchval(
                "SELECT 1 FROM questions WHERE legacy_id = $1", legacy_id
            )
            if not exists:
                not_found += 1

    await conn.close()

    print(f"Updated : {updated}")
    print(f"Already had MIN: {len(unique_ids) - updated - not_found}")
    print(f"Not found in DB : {not_found}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
