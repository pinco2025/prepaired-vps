#!/usr/bin/env python3
"""
Patch section_type in the source_info JSONB column to 'div4' for questions
whose legacy_id appears in matrix.json.

Run from the backend/ directory:
    python scripts/patch_section_type.py
    python scripts/patch_section_type.py --dry-run
    python scripts/patch_section_type.py --ids scripts/matrix.json --value div4

Requires DATABASE_URL in environment (or a .env file at backend/.env).
Safe to re-run — idempotent (skips rows already set to the target value).
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def patch(legacy_ids: list[str], target_value: str, dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("ERROR: DATABASE_URL is not set.")
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    print(f"Connecting to database…")
    conn = await asyncpg.connect(db_url)
    print(f"Connected.  legacy_ids={len(legacy_ids):,}  target='{target_value}'  dry_run={dry_run}\n")

    try:
        if dry_run:
            # Count how many rows would be touched (excluding those already set)
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM questions
                WHERE legacy_id = ANY($1::text[])
                  AND (source_info->>'section_type') IS DISTINCT FROM $2
                """,
                legacy_ids,
                target_value,
            )
            already = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM questions
                WHERE legacy_id = ANY($1::text[])
                  AND (source_info->>'section_type') = $2
                """,
                legacy_ids,
                target_value,
            )
            not_found_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT UNNEST($1::text[]) AS lid
                    EXCEPT
                    SELECT legacy_id FROM questions WHERE legacy_id = ANY($1::text[])
                ) t
                """,
                legacy_ids,
            )
            print(f"[dry-run] Would update : {count:,} rows")
            print(f"[dry-run] Already set  : {already:,} rows (skipped)")
            print(f"[dry-run] Not in DB    : {not_found_count:,} legacy_ids not found")
            return

        # Real update — only rows where section_type differs from the target
        result = await conn.fetch(
            """
            UPDATE questions
               SET source_info = jsonb_set(
                       COALESCE(source_info, '{}'),
                       '{section_type}',
                       to_jsonb($2::text)
                   )
             WHERE legacy_id = ANY($1::text[])
               AND (source_info->>'section_type') IS DISTINCT FROM $2
            RETURNING legacy_id
            """,
            legacy_ids,
            target_value,
        )

        updated_ids = {r["legacy_id"] for r in result}
        unmatched   = [lid for lid in legacy_ids if lid not in updated_ids]

        print(f"Updated  : {len(updated_ids):,} rows")
        print(f"Unmatched: {len(unmatched):,} legacy_ids not found in DB")
        if unmatched and len(unmatched) <= 20:
            print(f"  {unmatched}")
        elif unmatched:
            print(f"  First 20: {unmatched[:20]}  …")

        # Verify
        verify = await conn.fetchval(
            "SELECT COUNT(*) FROM questions WHERE legacy_id = ANY($1::text[]) AND source_info->>'section_type' = $2",
            legacy_ids,
            target_value,
        )
        print(f"\nVerification: {verify:,} / {len(legacy_ids):,} target rows now have section_type='{target_value}'")

    finally:
        await conn.close()
        print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Patch source_info.section_type for a list of legacy_ids."
    )
    parser.add_argument(
        "--ids",
        default="scripts/matrix.json",
        help="Path to JSON file containing an array of legacy_ids (default: scripts/matrix.json)",
    )
    parser.add_argument(
        "--value",
        default="div4",
        help="section_type value to set (default: div4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to DB",
    )
    args = parser.parse_args()

    ids_path = Path(args.ids)
    if not ids_path.exists():
        sys.exit(f"ERROR: IDs file not found: {ids_path}")

    with ids_path.open(encoding="utf-8") as f:
        legacy_ids = json.load(f)

    if not isinstance(legacy_ids, list):
        sys.exit("ERROR: IDs file must contain a JSON array of strings.")

    legacy_ids = [str(x) for x in legacy_ids if x]
    print(f"Loaded {len(legacy_ids):,} legacy_ids from {ids_path}")

    asyncio.run(patch(legacy_ids, args.value, args.dry_run))


if __name__ == "__main__":
    main()
