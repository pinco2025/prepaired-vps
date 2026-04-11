#!/usr/bin/env python3
"""
Patch used_in[] on questions from test_id in CSV metadata.

For every row in questions.csv where metadata.test_id is set,
append that test_id to the used_in TEXT[] column for the matching
question (matched by id UUID).  Duplicates are silently ignored.

Run from the backend/ directory:
    python scripts/patch_used_in_from_csv.py --csv /path/to/questions.csv

Requires DATABASE_URL in environment (same .env as the FastAPI app).
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

BATCH_SIZE = 200

# Append test_id only if it isn't already in the array.
PATCH_SQL = """
    UPDATE questions
    SET used_in = array(
        SELECT DISTINCT unnest(array_append(used_in, $2::text))
    )
    WHERE id = $1::uuid
      AND NOT ($2::text = ANY(used_in))
"""


async def main(csv_path: str) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("ERROR: DATABASE_URL environment variable not set.")

    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    print("Connecting to database…")
    conn = await asyncpg.connect(db_url)
    print("Connected.\n")

    # Collect (id, test_id) pairs from the CSV
    pairs: list[tuple[str, str]] = []
    skipped = 0

    csv_file = Path(csv_path)
    if not csv_file.exists():
        sys.exit(f"ERROR: CSV file not found: {csv_path}")

    print(f"Reading {csv_file} …")
    with csv_file.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw_meta = row.get("metadata") or "{}"
            try:
                meta = json.loads(raw_meta)
            except json.JSONDecodeError:
                skipped += 1
                continue

            test_id = meta.get("test_id")
            if test_id:
                pairs.append((row["id"], str(test_id)))

    print(f"  Found {len(pairs):,} rows with test_id  ({skipped} skipped — bad JSON)\n")

    if not pairs:
        print("Nothing to patch. Exiting.")
        await conn.close()
        return

    # Patch in batches
    updated = 0
    for i in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[i : i + BATCH_SIZE]
        async with conn.transaction():
            for question_id, test_id in batch:
                status = await conn.execute(PATCH_SQL, question_id, test_id)
                # status is e.g. "UPDATE 1" or "UPDATE 0"
                if status == "UPDATE 1":
                    updated += 1
        done = min(i + BATCH_SIZE, len(pairs))
        print(f"  Processed {done:,}/{len(pairs):,} …", end="\r")

    print(f"\n  {updated:,} rows updated, {len(pairs) - updated:,} already had the test_id.\n")

    await conn.close()
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Patch used_in[] on questions from test_id in CSV metadata."
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to questions.csv",
    )
    args = parser.parse_args()
    asyncio.run(main(args.csv))
